"""Admin panel: user management, live sessions, audit/analytics, server
management and LDAP configuration (directory calls mocked).

Never called here, even as admin: /admin/server/restart (os._exit) and the
postgres/redis *save* routes (they rewrite config.env and swap connections).
"""
import uuid
from unittest.mock import patch

import pytest

from src.db.tables import AuditLog, LDAPGroup, LDAPServer, User
from src.encryption import decrypt

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def get_user(session_scope, user_id):
	with session_scope() as s:
		u = s.get(User, user_id)
		if u:
			s.expunge(u)
		return u


# ── User management ──────────────────────────────────────────────────────────

def test_panel_and_users_page(admin, client_for):
	c = client_for(admin)
	assert c.get("/admin").headers["Location"] == "/admin/users"
	assert c.get("/admin/users").status_code == 200


@pytest.mark.parametrize("action,expected", [
	("approve", {"is_approved": True, "is_active": True}),
	("disable", {"is_active": False}),
	("promote", {"role": "admin", "is_approved": True}),
	("demote", {"role": "user"}),
])
def test_user_actions(admin, client_for, make_user, session_scope, action,
                      expected):
	target = make_user(approved=False, active=False) if action == "approve" \
		else make_user(role="admin" if action == "demote" else "user")
	client_for(admin).post(f"/admin/users/{target.id}/{action}")
	user = get_user(session_scope, target.id)
	for field, value in expected.items():
		assert getattr(user, field) == value, field
	with session_scope() as s:
		assert s.query(AuditLog).filter_by(action=f"user.{action}",
		                                   object_id=target.id).count() == 1


def test_admin_cannot_disable_or_delete_self(admin, client_for, session_scope):
	c = client_for(admin)
	c.post(f"/admin/users/{admin.id}/disable")
	c.post(f"/admin/users/{admin.id}/delete")
	assert get_user(session_scope, admin.id).is_active is True


def test_factory_admin_is_untouchable(admin, client_for, make_user,
                                      session_scope):
	factory = make_user(username="admin", role="admin")
	client_for(admin).post(f"/admin/users/{factory.id}/demote")
	client_for(admin).post(f"/admin/users/{factory.id}/delete")
	assert get_user(session_scope, factory.id).role == "admin"


def test_bulk_action(admin, client_for, make_user, session_scope):
	a, b = make_user(), make_user()
	client_for(admin).post("/admin/users/bulk/disable",
	                       data={"user_ids": f"{a.id},{b.id},{admin.id}"})
	assert not get_user(session_scope, a.id).is_active
	assert not get_user(session_scope, b.id).is_active
	assert get_user(session_scope, admin.id).is_active  # self skipped


def test_live_sessions_list_and_kick(app, admin, client_for, make_user):
	target = make_user()
	redis = app.backend.redis.client
	redis.set(f"user_session:{target.id}", "sid-123", ex=3600)
	redis.set("redis_session:sid-123", "session-data")
	c = client_for(admin)
	assert target.username in c.get("/admin/sessions").get_data(as_text=True)
	assert c.post(f"/admin/sessions/{target.id}/kick").json["status"] == "ok"
	assert redis.get(f"user_session:{target.id}") is None
	assert redis.get("redis_session:sid-123") is None
	assert c.post(f"/admin/sessions/{uuid.uuid4()}/kick").status_code == 404


# ── Audit + analytics ────────────────────────────────────────────────────────

def test_audit_page_filters(admin, client_for, make_user, session_scope):
	with session_scope() as s:
		s.add_all([AuditLog(actor_username="alice", action="inventory.create"),
		           AuditLog(actor_username="bob", action="auth.login",
		                    success=False)])
	c = client_for(admin)
	html = c.get("/admin/audit?actor=alice").get_data(as_text=True)
	assert "inventory.create" in html and "bob" not in html
	assert "alice" not in c.get("/admin/audit?success=false").get_data(as_text=True)


def test_audit_query_is_allowlisted(admin, client_for, session_scope):
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


def test_admin_analytics_page_and_job_count(app, admin, client_for):
	c = client_for(admin)
	assert c.get("/admin/analytics").status_code == 200
	app.backend.redis.client.set("netrollout:active_count", 3)
	assert c.get("/admin/active_job_count").json["count"] == 3


# ── Server management ────────────────────────────────────────────────────────

def test_server_page_renders(admin, client_for):
	assert client_for(admin).get("/admin/server").status_code == 200


def test_postgres_test_endpoint_reports_unreachable_server(admin, client_for):
	dead = client_for(admin).post("/admin/server/postgres/test", json={
		"host": "127.0.0.1", "port": "5999", "name": "none", "user": "u",
		"password": "p"})
	assert dead.json["status"] == "error"


def test_postgres_test_endpoint_validates_fields(admin, client_for):
	missing = client_for(admin).post("/admin/server/postgres/test",
	                                 json={"host": "x"})
	assert missing.json["status"] == "error"


def test_redis_test_endpoint(admin, client_for):
	resp = client_for(admin).post("/admin/server/redis/test", json={
		"host": "127.0.0.1", "port": "6999"})
	assert resp.json["status"] == "error"


# ── LDAP configuration ───────────────────────────────────────────────────────

LDAP_FORM = {"label": "corp", "ip": "ldap.test", "port": "389",
             "base_dn": "dc=corp", "cn_identifier": "sAMAccountName",
             "bind_type": "regular", "bind_dn": "cn=svc,dc=corp",
             "use_ssl": "false", "is_active": "true",
             "bind_password": "bind-secret"}


def only_server(session_scope):
	with session_scope() as s:
		srv = s.query(LDAPServer).one()
		s.expunge(srv)
		return srv


def test_ldap_server_crud_encrypts_bind_password(admin, client_for,
                                                 session_scope):
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
