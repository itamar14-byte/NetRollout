"""Admin -> Audit log: the page and its filters; the rows AuditTrail writes."""
import uuid

from src.audit import Actor, AuditAction
from src.backup.schedule import system_audit
from src.db.tables import AuditLog
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
