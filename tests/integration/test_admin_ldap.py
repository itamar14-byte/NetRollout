"""Server Management -> LDAP: the servers (bind password encrypted, bad fields
refused in words), import, groups - the directory calls mocked; the real
directory is test_ldap_directory.py."""
import uuid
from unittest.mock import patch

import pytest

from src.db.tables import AuditLog, LDAPGroup, LDAPServer, User
from src.encryption import decrypt
from tests.integration.test_admin_users import admin  # noqa: F401 - fixture


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


@pytest.mark.parametrize("bad, message", [
	({"port": "abc"}, "The port is a number from 1 to 65535."),
	({"port": "²2"}, "The port is a number from 1 to 65535."),
	({"bind_type": "weird"}, "The bind type is regular or simple."),
])
def test_ldap_server_form_refuses_a_bad_field_in_words(admin, client_for,
                                                       session_scope, bad, message):
	"""The LDAP server form's port and bind type are checked: a bad one is
	refused with the reason (422) on add and on save, changing nothing."""
	c = client_for(admin)
	resp = c.post("/admin/server/ldap/new", data={**LDAP_FORM, **bad})
	assert (resp.status_code, resp.json["message"]) == (422, message)
	with session_scope() as s:
		assert s.query(LDAPServer).count() == 0
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	srv = only_server(session_scope)
	resp = c.post(f"/admin/server/ldap/{srv.id}/save", data={**LDAP_FORM, **bad})
	assert (resp.status_code, resp.json["message"]) == (422, message)
	same = only_server(session_scope)
	assert (same.port, same.bind_type) == (srv.port, srv.bind_type)


def test_ldap_import_skips_malformed_items(admin, client_for, session_scope):
	"""An import list with malformed items (no type, not an object) imports the
	good ones and counts the rest as skipped; a body that isn't a list is
	refused (422)."""
	c = client_for(admin)
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	sid = only_server(session_scope).id
	resp = c.post(f"/admin/server/ldap/{sid}/import", json=[
		{"type": "user", "username": "jdoe"}, {"username": "no-type"}, "text"])
	assert (resp.json["users_created"], resp.json["skipped"]) == (1, 2)
	assert c.post(f"/admin/server/ldap/{sid}/import",
	              json={"type": "user"}).status_code == 422


def test_ldap_import_skips_a_user_that_exists_in_another_case(
		admin, client_for, session_scope, make_user):
	"""Importing "Alice" when "alice" exists - or twice in one list in two cases - skips
	the duplicate, as sign-in and account creation compare usernames: it created a
	second account (the two sign in as one another's)."""
	make_user(username="alice")
	c = client_for(admin)
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	sid = only_server(session_scope).id
	resp = c.post(f"/admin/server/ldap/{sid}/import", json=[
		{"type": "user", "username": "Alice"},
		{"type": "user", "username": "BOB"}, {"type": "user", "username": "bob"}])
	assert (resp.json["users_created"], resp.json["skipped"]) == (1, 2)
	with session_scope() as s:
		names = sorted(u.username.lower() for u in s.query(User))
	assert names.count("alice") == 1 and names.count("bob") == 1


def test_ldap_import_skips_the_reserved_username(admin, client_for, session_scope):
	"""Importing a directory user named like Grafana's own administrator (any
	capitals) skips it - no NetRollout account may carry that name."""
	c = client_for(admin)
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	sid = only_server(session_scope).id
	resp = c.post(f"/admin/server/ldap/{sid}/import", json=[
		{"type": "user", "username": "NetRollout-Grafana-Admin"},
		{"type": "user", "username": "carol"}])
	assert (resp.json["users_created"], resp.json["skipped"]) == (1, 1)
	with session_scope() as s:
		assert s.query(User).filter(User.username.ilike("netrollout-grafana-admin")).count() == 0


def test_ldap_directory_calls_are_delegated(admin, client_for, session_scope):
	"""The LDAP test, test-user, fetch-DN and explore routes hand off to the
	directory functions; an unknown server is 404."""
	c = client_for(admin)
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	sid = only_server(session_scope).id
	base = "src.accounts.ldap.Directory"
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
	password. (The directory's answer on the user's groups is mocked: none.)"""
	c = client_for(admin)
	c.post("/admin/server/ldap/new", data=LDAP_FORM)
	sid = only_server(session_scope).id
	items = [{"type": "user", "username": "jdoe"},
	         {"type": "group", "dn": "cn=netops,dc=corp", "label": "netops"}]
	with patch("src.accounts.ldap.Directory.matching_rules", return_value=[]):
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


# ── The role a group rule gives ──────────────────────────────────────────────

def server_with_rule(session_scope, bind_type="regular", active=True):
	"""(server id, rule id): an LDAP server and one operator group rule."""
	with session_scope() as s:
		srv = LDAPServer(name="corp", host="ldap.test", port=389, base_dn="dc=corp",
		                 bind_type=bind_type, bind_dn="cn=svc", is_active=True)
		s.add(srv)
		s.flush()
		rule = LDAPGroup(group_dn="cn=netops,dc=corp", label="netops", role="operator",
		                 is_active=active, ldap_server_id=srv.id)
		s.add(rule)
		s.flush()
		return srv.id, rule.id


def test_a_group_rule_can_give_the_admin_role(admin, client_for, session_scope):
	"""An admin sets a rule's role to admin and back (audited
	admin.ldap_group_role with from / to); anything but operator or admin is
	refused (422), the rule unchanged."""
	sid, gid = server_with_rule(session_scope)
	c = client_for(admin, xhr=True)
	resp = c.post(f"/admin/server/ldap/{sid}/groups/{gid}/role", data={"role": "admin"})
	assert resp.json == {"status": "ok", "role": "admin"}
	assert c.get(f"/admin/server/ldap/{sid}/groups").json[0]["role"] == "admin"
	bad = c.post(f"/admin/server/ldap/{sid}/groups/{gid}/role", data={"role": "root"})
	assert bad.status_code == 422
	with session_scope() as s:
		assert s.get(LDAPGroup, gid).role == "admin"
		rows = [a.detail for a in s.query(AuditLog).filter_by(action="admin.ldap_group_role")]
	assert rows == [{"from": "operator", "to": "admin"}]
	missing = c.post(f"/admin/server/ldap/{sid}/groups/{uuid.uuid4()}/role",
	                 data={"role": "admin"})
	assert missing.status_code == 404


@pytest.mark.parametrize("action", ["promote", "demote"])
def test_promote_and_demote_are_refused_where_the_directory_sets_the_role(
		action, admin, client_for, make_user, session_scope):
	"""A directory user whose server has a service account and an active group
	rule: Promote / Demote (alone or ticked with others) are refused in words -
	the next sign-in would undo them - and nothing changes."""
	sid, _ = server_with_rule(session_scope)
	role = "admin" if action == "demote" else "operator"
	user = make_user(role=role, auth_type="ldap", ldap_server_id=sid)
	other = make_user(role=role)
	c = client_for(admin)
	c.post(f"/admin/users/{user.id}/{action}")
	c.post(f"/admin/users/bulk/{action}", data={"user_ids": f"{other.id},{user.id}"})
	with c.session_transaction() as s:
		shown = [m for _, m in s.pop("_flashes", [])]
	expected = (f"The role of {user.username} comes from the directory's group rules "
	            f"(User Management -> Group Rules), set at each sign-in - change the rule, "
	            f"or the user's groups in the directory.")
	assert shown == [expected, expected]
	with session_scope() as s:
		assert (s.get(User, user.id).role, s.get(User, other.id).role) == (role, role)


@pytest.mark.parametrize("server", ["simple_bind", "no_active_rule"])
def test_promote_works_where_the_directory_cant_set_the_role(
		server, admin, client_for, make_user, session_scope):
	"""A directory without a service account, or without an active group rule,
	can't tell a role: Promote applies as for any account."""
	sid, _ = server_with_rule(session_scope,
	                          bind_type="simple" if server == "simple_bind" else "regular",
	                          active=server != "no_active_rule")
	user = make_user(auth_type="ldap", ldap_server_id=sid)
	client_for(admin).post(f"/admin/users/{user.id}/promote")
	with session_scope() as s:
		assert s.get(User, user.id).role == "admin"
