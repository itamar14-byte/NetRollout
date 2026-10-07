"""Admin panel: user management, live sessions, audit/analytics, server
management and LDAP configuration (directory calls mocked).

Never called here, even as admin: /admin/server/restart (os._exit). The
redis *save* route runs with the connection swap stubbed out; the database
move routes are in test_db_move_routes.py.
"""
import html
import json
import re
import time
import uuid
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from src.db.tables import AuditLog, LDAPGroup, LDAPServer, User
from src.encryption import decrypt, encrypt
from tests.integration.conftest import TEST_PASSWORD

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def get_user(session_scope, user_id):
	"""The user row by id, detached from its session (None when gone)."""
	with session_scope() as s:
		u = s.get(User, user_id)
		if u:
			s.expunge(u)
		return u


# ── User management ──────────────────────────────────────────────────────────

def test_panel_and_users_page(admin, client_for):
	"""/admin redirects to /admin/users, which renders."""
	c = client_for(admin)
	assert c.get("/admin").headers["Location"] == "/admin/users"
	assert c.get("/admin/users").status_code == 200


@pytest.mark.parametrize("action,expected", [
	("approve", {"is_approved": True, "is_active": True}),
	("disable", {"is_active": False}),
	("promote", {"role": "admin", "is_approved": True}),
	("demote", {"role": "operator"}),
])
def test_user_actions(admin, client_for, make_user, session_scope, action,
                      expected):
	"""Each user action sets its fields and is audited once as user.<action>
	(cases: approve, disable, promote, demote)."""
	target = make_user(approved=False, active=False) if action == "approve" \
		else make_user(role="admin" if action == "demote" else "operator")
	client_for(admin).post(f"/admin/users/{target.id}/{action}")
	user = get_user(session_scope, target.id)
	for field, value in expected.items():
		assert getattr(user, field) == value, field
	with session_scope() as s:
		assert s.query(AuditLog).filter_by(action=f"user.{action}",
		                                   object_id=target.id).count() == 1


def test_admin_cannot_disable_or_delete_self(admin, client_for, session_scope):
	"""An admin's disable and delete of their own account leave it in place
	and active."""
	c = client_for(admin)
	c.post(f"/admin/users/{admin.id}/disable")
	c.post(f"/admin/users/{admin.id}/delete")
	assert get_user(session_scope, admin.id).is_active is True


def test_factory_admin_is_untouchable(admin, client_for, make_user,
                                      session_scope):
	"""The factory "admin" account can't be demoted or deleted: it stays an
	admin."""
	factory = make_user(username="admin", role="admin")
	client_for(admin).post(f"/admin/users/{factory.id}/demote")
	client_for(admin).post(f"/admin/users/{factory.id}/delete")
	assert get_user(session_scope, factory.id).role == "admin"


def test_bulk_action(admin, client_for, make_user, session_scope):
	"""A bulk disable disables the selected users but skips the admin doing it."""
	a, b = make_user(), make_user()
	client_for(admin).post("/admin/users/bulk/disable",
	                       data={"user_ids": f"{a.id},{b.id},{admin.id}"})
	assert not get_user(session_scope, a.id).is_active
	assert not get_user(session_scope, b.id).is_active
	assert get_user(session_scope, admin.id).is_active  # self skipped


def test_bulk_reset_2fa(admin, app, client_for, make_user, session_scope):
	"""Bulk Reset 2FA: the Users page offers it and knows who has 2FA; both
	secrets are cleared, one audit entry names both users, and the next
	sign-in goes to enrollment."""
	a, b = make_user(), make_user()
	with session_scope() as s:
		for uid in (a.id, b.id):
			s.get(User, uid).otp_secret = encrypt("JBSWY3DPEHPK3PXP")
	# the Users page offers the action and knows who has 2FA
	page = client_for(admin).get("/admin/users").get_data(as_text=True)
	assert 'id="btnReset2FA"' in page and '"has_2fa": true' in page
	client_for(admin).post("/admin/users/bulk/reset_2fa",
	                       data={"user_ids": f"{a.id},{b.id}"})
	assert get_user(session_scope, a.id).otp_secret is None
	assert get_user(session_scope, b.id).otp_secret is None
	with session_scope() as s:
		entry = s.query(AuditLog).filter_by(action="user.bulk_reset_2fa").one()
		assert sorted(entry.detail["users"]) == sorted([a.username, b.username])
	# next sign-in goes to enrollment, not verification
	resp = app.test_client().post("/login", data={"username": a.username,
	                                              "password": TEST_PASSWORD})
	assert resp.headers["Location"] == "/otp_enroll"


def test_reset_2fa_is_admin_only(client_for, make_user, session_scope):
	"""An operator's bulk Reset 2FA leaves the user's 2FA secret in place."""
	victim = make_user()
	with session_scope() as s:
		s.get(User, victim.id).otp_secret = encrypt("JBSWY3DPEHPK3PXP")
	client_for(make_user()).post("/admin/users/bulk/reset_2fa",
	                             data={"user_ids": str(victim.id)})
	assert get_user(session_scope, victim.id).otp_secret is not None


def test_live_sessions_list_and_kick(app, admin, client_for, make_user):
	"""Live Sessions lists a signed-in user; Kick ends the session (its key
	gone from Redis); kicking a user without a session is 404."""
	target = make_user()
	browser = client_for(target)
	browser.get("/dashboard")
	sid = browser.get_cookie("session").value
	c = client_for(admin)
	assert target.username in c.get("/admin/sessions").get_data(as_text=True)
	assert c.post(f"/admin/sessions/{target.id}/kick").json["status"] == "ok"
	assert app.backend.redis.client.get(f"redis_session:{sid}") is None
	assert c.post(f"/admin/sessions/{uuid.uuid4()}/kick").status_code == 404


def test_kick_signs_the_user_out_of_every_browser(admin, client_for, make_user):
	"""A user signed in on two computers is signed out of both by one Kick:
	each one's next page goes to the sign-in page, and the open page's
	background session check is redirected there too (so it leaves within
	30 s)."""
	target = make_user()
	office, laptop = client_for(target), client_for(target)
	assert office.get("/dashboard").status_code == 200
	assert laptop.get("/dashboard").status_code == 200
	assert client_for(admin).post(f"/admin/sessions/{target.id}/kick").status_code == 200
	for browser in (office, laptop):
		assert browser.get("/dashboard").status_code == 302
		# the open page's 30 s check: redirected to the sign-in page, which
		# makes the page leave (_idle_timeout.html: r.redirected)
		check = browser.get("/account/session", headers={"X-NR-Background": "1"})
		assert (check.status_code, check.headers["Location"]) == (
			302, "/?next=%2Faccount%2Fsession")


def test_live_sessions_leave_out_sessions_that_ended(admin, client_for, make_user):
	"""A session past its idle limit (ended, though its browser hasn't come
	back to be told) isn't listed on Live Sessions, nor shown as signed in on
	Users; a live one is."""
	active, idle = make_user(), make_user()
	client_for(active).get("/dashboard")
	gone = client_for(idle)
	gone.get("/dashboard")
	with gone.session_transaction() as s:      # last seen 20 minutes ago
		s["nr_last_active"] = time.time() - 20 * 60
	c = client_for(admin)
	page = c.get("/admin/sessions").get_data(as_text=True)
	assert active.username in page and idle.username not in page
	rows = [json.loads(html.unescape(raw)) for raw in re.findall(
		r"data-user='([^']*)'", c.get("/admin/users").get_data(as_text=True))]
	signed_in = {r["username"]: r["has_session"] for r in rows}
	assert signed_in[active.username] is True
	assert signed_in[idle.username] is False


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


def test_audit_query_is_allowlisted(admin, client_for, session_scope):
	"""The admin analytics query filters the audit log on an allowed field; a
	field outside the allowlist gets 400."""
	with session_scope() as s:
		s.add(AuditLog(actor_username="alice", action="auth.login",
		               success=False))
	c = client_for(admin)
	resp = c.post("/admin/analytics/query", json={"rules": {
		"field": "success", "operator": "equal", "value": "false"}})
	assert [r["actor_username"] for r in resp.json["rows"]] == ["alice"]
	bad = c.post("/admin/analytics/query", json={"rules": {
		"field": "detail", "operator": "contains", "value": "x"}})
	assert bad.status_code == 400


def test_admin_analytics_page_and_job_count(app, admin, client_for,
                                            monkeypatch):
	"""The analytics page renders, and the active job count is the
	orchestrator's running + queued."""
	c = client_for(admin)
	assert c.get("/admin/analytics").status_code == 200
	# The orchestrator's own count (running + queued), not a Redis counter
	monkeypatch.setattr(app.orchestrator, "counts",
	                    lambda: {"running": 2, "queued": 1})
	body = c.get("/admin/active_job_count").json
	assert (body["count"], body["running"], body["queued"]) == (3, 2, 1)


def test_terminate_session_signs_the_user_out_everywhere(
		admin, make_user, client_for, app):
	"""Terminate Session removes all the user's sessions (both browsers): the
	next page and the background session check redirect to the sign-in."""
	target = make_user()
	browsers = [client_for(target) for _ in range(2)]
	for b in browsers:
		b.get("/dashboard")
	sids = [b.get_cookie("session").value for b in browsers]
	app.backend.redis.client.set(f"user_session:{target.id}", sids[-1])
	client_for(admin).post(f"/admin/users/{target.id}/terminate_session")
	assert not any(app.backend.redis.client.exists(f"redis_session:{s}")
	               for s in sids)
	# ...and really out: the next page goes to sign-in, and the page's own
	# background check (every 30 s) is refused too
	for b in browsers:
		assert b.get("/dashboard").headers["Location"].startswith("/?next=")
		check = b.get("/account/session", headers={"X-NR-Background": "1"})
		assert check.status_code == 302 and check.headers["Location"].startswith("/?next=")


# ── Server management ────────────────────────────────────────────────────────

def test_server_page_renders(admin, client_for):
	"""Server Management renders for an admin."""
	assert client_for(admin).get("/admin/server").status_code == 200


def test_redis_test_endpoint(admin, client_for):
	"""Testing a Redis address where nothing listens answers status error."""
	resp = client_for(admin).post("/admin/server/redis/test", json={
		"host": "127.0.0.1", "port": "6999"})
	assert resp.json["status"] == "error"


@pytest.fixture
def switch_writes_only(app, monkeypatch):
	"""A save swaps the shared app's connection; stub only the swap, so the
	route → backend → config/runtime.env path runs for real. Yields the
	runtime.env path, removed before and after."""
	monkeypatch.setattr(app.backend.redis, "reload_db", lambda config: None)
	runtime_env = app.backend._CONFIG_ENV
	runtime_env.unlink(missing_ok=True)
	yield runtime_env
	runtime_env.unlink(missing_ok=True)


def test_save_routes_write_runtime_env(app, admin, client_for, switch_writes_only):
	"""Saving another Redis writes its keys to config/runtime.env, the unused
	ones blank, and keeps the bundled Redis's URL for the way back."""
	bundled = app.backend.redis.config.get_url()      # the test Redis counts as bundled
	client = client_for(admin)
	assert client.post("/admin/server/redis/save", json={
		"host": "cache.example.org", "port": "6380"}).json["status"] == "ok"
	# unused keys blank so nothing inherited wins; leaving the bundled Redis,
	# its address is kept for the way back
	assert dotenv_values(switch_writes_only) == {
		"REDIS_HOST": "cache.example.org", "REDIS_PORT": "6380",
		"REDIS_DB": "0", "REDIS_PASSWORD": "", "REDIS_URL": "",
		"NETROLLOUT_BUNDLED_REDIS_URL": bundled}


# ── LDAP configuration ───────────────────────────────────────────────────────

LDAP_FORM = {"label": "corp", "ip": "ldap.test", "port": "389",
             "base_dn": "dc=corp", "cn_identifier": "sAMAccountName",
             "bind_type": "regular", "bind_dn": "cn=svc,dc=corp",
             "use_ssl": "false", "is_active": "true",
             "bind_password": "bind-secret"}


def only_server(session_scope):
	"""The one LDAP server row, detached from its session."""
	with session_scope() as s:
		srv = s.query(LDAPServer).one()
		s.expunge(srv)
		return srv


def test_ldap_server_crud_encrypts_bind_password(admin, client_for,
                                                 session_scope):
	"""LDAP server create / list / save / delete: the bind password is stored
	encrypted, never listed, kept when saved blank; delete removes the row."""
	c = client_for(admin)
	assert c.post("/admin/server/ldap/new", data=LDAP_FORM).json["status"] == "ok"
	srv = only_server(session_scope)
	assert decrypt(srv.bind_password) == "bind-secret"
	listing = c.get("/admin/server/ldap").json
	assert listing[0]["name"] == "corp" and "bind_password" not in listing[0]

	c.post(f"/admin/server/ldap/{srv.id}/save",
	       data={**LDAP_FORM, "label": "corp2", "bind_password": ""})
	saved = only_server(session_scope)
	assert saved.name == "corp2" and decrypt(saved.bind_password) == "bind-secret"

	assert c.post(f"/admin/server/ldap/{srv.id}/delete").json["status"] == "ok"
	with session_scope() as s:
		assert s.query(LDAPServer).count() == 0


def test_ldap_directory_calls_are_delegated(admin, client_for, session_scope):
	"""The LDAP test, test-user, fetch-DN and explore routes hand off to the
	directory functions; an unknown server is 404."""
	c = client_for(admin)
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	sid = only_server(session_scope).id
	base = "src.webapp.blueprints.admin_servers"
	with patch(f"{base}.test_connection", return_value={"status": "ok"}), \
			patch(f"{base}.test_user", return_value={"status": "ok"}), \
			patch(f"{base}.fetch_base_dn", return_value={"status": "ok"}), \
			patch(f"{base}.walk_tree", return_value={"status": "ok"}):
		assert c.post(f"/admin/server/ldap/{sid}/test").json["status"] == "ok"
		assert c.post(f"/admin/server/ldap/{sid}/test_user", data={
			"username": "u", "password": "p"}).json["status"] == "ok"
		assert c.post(f"/admin/server/ldap/{sid}/fetch_dn").json["status"] == "ok"
		assert c.post(f"/admin/server/ldap/{sid}/explore").json["status"] == "ok"
	missing = c.post(f"/admin/server/ldap/{uuid.uuid4()}/test")
	assert missing.status_code == 404


def test_ldap_import_and_group_rules(admin, client_for, session_scope):
	"""Importing a user and a group creates them once (again: both skipped); a
	group can be toggled off and deleted; the user is an LDAP account with no
	password."""
	c = client_for(admin)
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	sid = only_server(session_scope).id
	items = [{"type": "user", "username": "jdoe"},
	         {"type": "group", "dn": "cn=netops,dc=corp", "label": "netops"}]
	resp = c.post(f"/admin/server/ldap/{sid}/import", json=items)
	assert (resp.json["users_created"], resp.json["groups_created"]) == (1, 1)
	again = c.post(f"/admin/server/ldap/{sid}/import", json=items)
	assert again.json["skipped"] == 2

	(group,) = c.get(f"/admin/server/ldap/{sid}/groups").json
	gid = group["id"]
	toggled = c.post(f"/admin/server/ldap/{sid}/groups/{gid}/toggle")
	assert toggled.json["is_active"] is False
	assert c.post(f"/admin/server/ldap/{sid}/groups/{gid}/delete") \
		       .json["status"] == "ok"
	with session_scope() as s:
		assert s.query(LDAPGroup).count() == 0
		user = s.query(User).filter_by(username="jdoe").one()
		assert user.auth_type == "ldap" and user.password_hash is None
