"""Admin -> Audit log: the page and its filters; the rows AuditTrail writes."""
import csv
import io
import json
import uuid
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

from src.audit import Actor, AuditAction
from src.backup.schedule import system_audit
from src.db.tables import AuditLog
from src.webapp.blueprints.admin_audit import EXPORT_COLUMNS
from tests.integration.test_admin_users import admin  # noqa: F401 - fixture


# ── Audit + analytics ────────────────────────────────────────────────────────

def test_audit_page_filters(admin, client_for, make_user, session_scope):
	"""The audit page filters by actor and by success."""
	with session_scope() as s:
		s.add_all([AuditLog(actor_username="alice", action="inventory.create"),
		           AuditLog(actor_username="bob", action="auth.login",
		                    success=False)])
	c = client_for(admin)
	html = c.get("/admin/audit?actor=alice").get_data(as_text=True)
	assert "inventory.create" in html and "bob" not in html
	assert "alice" not in c.get("/admin/audit?success=false").get_data(as_text=True)


class _DetailAttributes(HTMLParser):
	"""The data-detail attribute of every audit row, as a browser reads it."""
	def __init__(self):
		super().__init__()
		self.details = []

	def handle_starttag(self, tag, attrs):
		attrs = dict(attrs)
		if tag == "tr" and "data-detail" in attrs:
			self.details.append(attrs["data-detail"])


def test_a_rows_detail_reaches_the_page_whole(admin, client_for, session_scope):
	"""View Detail reads the row's data-detail attribute: it holds the stored
	detail as JSON, whole - quotes of either kind included - so the page can
	show it."""
	detail = {"reason": "ldap_bind_failed", "note": 'it\'s "quoted"'}
	with session_scope() as s:
		s.add(AuditLog(actor_username="alice", action="auth.login", success=False,
		               detail=detail))
	parser = _DetailAttributes()
	parser.feed(client_for(admin).get("/admin/audit").get_data(as_text=True))
	assert detail in [json.loads(d) for d in parser.details if d != "null"]


def test_each_row_shows_its_detail_in_one_line(admin, client_for, session_scope):
	"""Under the action, a row with a detail shows it in one line
	(audit.summary); a row without one shows none."""
	with session_scope() as s:
		s.add_all([AuditLog(actor_username="alice", action="auth.login", success=False,
		                    detail={"reason": "ldap_server_inactive", "ldap_server": "Lab"}),
		           AuditLog(actor_username="alice", action="auth.logout")])
	html = client_for(admin).get("/admin/audit").get_data(as_text=True)
	assert '<div class="audit-summary">reason: ldap_server_inactive (Lab)</div>' in html
	assert html.count('class="audit-summary"') == 1


def _columns(row):
	return {c: getattr(row, c) for c in ("actor_id", "actor_username", "action",
	                                     "object_type", "object_id", "object_label",
	                                     "success", "ip_address", "detail")}


def test_the_audit_trail_writes_every_column(app, admin, session_scope):
	"""A request's row (WebServices.audit: the signed-in user's id and name, the
	request's address) and the server's (system_audit: no id, "scheduler", no address)
	carry exactly the columns given, in their own committed session."""
	object_id = uuid.uuid4()
	with app.test_request_context("/", environ_base={"REMOTE_ADDR": "198.51.100.7"}):
		app.web.audit(AuditAction.SETTINGS_UPDATE, object_type="setting",
		              object_id=object_id, object_label="x", detail={"a": 1},
		              success=False, username="dana", actor_id=admin.id)
	system_audit(app.backend, AuditAction.BACKUP_FAILED, label="b.zip", success=False,
	             detail={"kind": "scheduled"})
	app.web.audit_trail.record(Actor.system("netrollout"), AuditAction.BACKUP_CREATED)
	with session_scope() as s:
		rows = {r.action: _columns(r) for r in s.query(AuditLog).filter(AuditLog.action.in_(
			[AuditAction.SETTINGS_UPDATE, AuditAction.BACKUP_FAILED,
			 AuditAction.BACKUP_CREATED]))}
	assert rows == {
		"settings.update": {"actor_id": admin.id, "actor_username": "dana",
		                    "action": "settings.update", "object_type": "setting",
		                    "object_id": object_id, "object_label": "x", "success": False,
		                    "ip_address": "198.51.100.7", "detail": {"a": 1}},
		"backup.failed": {"actor_id": None, "actor_username": "scheduler",
		                  "action": "backup.failed", "object_type": "backup",
		                  "object_id": None, "object_label": "b.zip", "success": False,
		                  "ip_address": None, "detail": {"kind": "scheduled"}},
		"backup.created": {"actor_id": None, "actor_username": "netrollout",
		                   "action": "backup.created", "object_type": None,
		                   "object_id": None, "object_label": None, "success": True,
		                   "ip_address": None, "detail": None},
	}


def test_audit_page_filters_by_day(admin, client_for, session_scope):
	"""?from= / ?to= keep the entries of those days, both included."""
	now = datetime.now()
	with session_scope() as s:
		s.add_all([AuditLog(actor_username="day_old_actor", action="auth.login",
		                    timestamp=now - timedelta(days=5)),
		           AuditLog(actor_username="day_new_actor", action="auth.login", timestamp=now)])
	c = client_for(admin)
	today, five_ago = date.today(), date.today() - timedelta(days=5)
	html = c.get(f"/admin/audit?from={today}").get_data(as_text=True)
	assert "day_new_actor" in html and "day_old_actor" not in html
	html = c.get(f"/admin/audit?from={five_ago}&to={five_ago}").get_data(as_text=True)
	assert "day_old_actor" in html and "day_new_actor" not in html


def test_audit_export_holds_every_entry_the_filters_let_through(admin, client_for, make_user,
                                                                session_scope):
	"""Export CSV holds every matching entry, past the page's 500, newest
	first, the detail as JSON on one line; admins only. The export is itself
	audited, after the file is made."""
	now = datetime.now()
	with session_scope() as s:
		s.add_all(AuditLog(actor_username="bulk", action="inventory.create",
		                   timestamp=now - timedelta(seconds=i)) for i in range(505))
		s.add(AuditLog(actor_username="bulk", action="inventory.delete",
		               timestamp=now + timedelta(seconds=1),
		               object_type="Inventory", object_label="=core", success=False,
		               detail={"why": "שלום"}))
	c = client_for(admin)
	assert c.get("/admin/audit?actor=bulk").get_data(as_text=True).count('class="audit-row"') == 500
	resp = c.get("/admin/audit/export.csv?actor=bulk")
	text = resp.get_data(as_text=True)
	assert text.startswith("\ufeff")
	rows = list(csv.reader(io.StringIO(text[1:])))
	assert rows[0] == EXPORT_COLUMNS and len(rows) == 1 + 506
	assert rows[1][2:] == ["inventory.delete", "Inventory", "'=core", "fail", "",
	                       '{"why": "שלום"}']
	with session_scope() as s:
		entry = s.query(AuditLog).filter_by(action="audit.exported").one()
		assert entry.detail == {"actor": "bulk", "rows": 506}
	assert client_for(make_user()).get("/admin/audit/export.csv").status_code == 302
