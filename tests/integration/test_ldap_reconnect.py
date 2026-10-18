"""Directory accounts whose LDAP server was deleted (its users kept):
reconnecting them to another server - User Management's Reconnect and the
directory import's option -, and Enable / Approve / Promote refused until
then. Against the test directory (jdoe, bsmith, alice; netops = jdoe and
bsmith; two entries named dup)."""
import uuid

import pytest

import src.encryption as enc
from src.db.tables import AuditLog, LDAPGroup, LDAPServer, User
from tests.integration.conftest import LDAP_BASE, LDAP_GROUP_DN, LDAP_SERVICE_DN, LDAP_SERVICE_PW
from tests.integration.test_admin_users import admin  # noqa: F401 - fixture
from tests.integration.test_ldap_directory import ADMINS_DN, admins_group  # noqa: F401 - fixture

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def add_server(session_scope, port, bind_type="regular", rules=(), active=True):
	""":returns: the id of an LDAP server for the test directory, with these
	(group_dn, role) rules, active"""
	with session_scope() as s:
		srv = LDAPServer(name=f"corp-{uuid.uuid4().hex[:4]}", host="127.0.0.1", port=port,
		                 base_dn=LDAP_BASE, cn_identifier="uid", bind_type=bind_type,
		                 bind_dn=LDAP_SERVICE_DN if bind_type == "regular" else None,
		                 bind_password=enc.encrypt(LDAP_SERVICE_PW) if bind_type == "regular" else None,
		                 is_active=active)
		s.add(srv)
		s.flush()
		for dn, role in rules:
			s.add(LDAPGroup(group_dn=dn, label=dn.split(",")[0], role=role, ldap_server_id=srv.id))
		return srv.id


def add_orphan(session_scope, username, role="operator"):
	"""A directory account whose server was deleted, its users kept: LDAP, no
	server, disabled, with its old email and name."""
	with session_scope() as s:
		user = User(username=username, role=role, auth_type="ldap", ldap_server_id=None,
		            is_approved=True, is_active=False, password_hash=None,
		            email=f"{username}@test.local", full_name=username)
		s.add(user)
		s.flush()
		return user.id


def account(session_scope, user_id):
	with session_scope() as s:
		u = s.get(User, user_id)
		return (u.ldap_server_id, u.is_active, u.role, u.email, u.full_name)


def reconnect(client_for, admin, user_ids, server_id):
	return client_for(admin).post("/admin/users/reconnect", json={
		"user_ids": [str(u) for u in user_ids], "server_id": str(server_id)})


@pytest.mark.ldap
def test_a_kept_account_reconnects_disabled_with_the_directorys_details(
		admin, ldap_directory, make_user, client_for, session_scope):
	"""jdoe, kept without a directory, is linked to the new server; it takes
	the directory's email and name and stays disabled (the admin enables it).
	Audited with the server and the names."""
	srv = add_server(session_scope, ldap_directory)
	jdoe = add_orphan(session_scope, "jdoe")
	resp = reconnect(client_for, admin, [jdoe], srv)
	assert resp.status_code == 200 and resp.json["users"] == ["jdoe"]
	assert account(session_scope, jdoe) == (srv, False, "operator", "jdoe@corp.test", "John Doe")
	with session_scope() as s:
		entry = s.query(AuditLog).filter_by(action="user.reconnected").one()
		assert (entry.object_id, entry.detail) == (srv, {"users": ["jdoe"], "admins": []})


@pytest.mark.ldap
def test_a_name_the_directory_lacks_refuses_them_all(admin, ldap_directory, make_user,
                                                     client_for, session_scope):
	"""jdoe exists there, ghost doesn't and dup is two entries: refused in
	words naming both, nothing reconnected - jdoe neither."""
	srv = add_server(session_scope, ldap_directory)
	users = [add_orphan(session_scope, name) for name in ("jdoe", "ghost", "dup")]
	resp = reconnect(client_for, admin, users, srv)
	assert resp.status_code == 422
	assert "Not in corp-" in resp.json["message"] and ": dup, ghost." in resp.json["message"]
	assert "Nothing was reconnected." in resp.json["message"]
	assert all(account(session_scope, u)[0] is None for u in users)


def test_a_server_without_a_service_account_cant_check_the_names(
		admin, make_user, client_for, session_scope):
	"""A server binding as the user signing in can't look names up: refused,
	nothing changed - and so is a server that is switched off."""
	jdoe = add_orphan(session_scope, "jdoe")
	simple = add_server(session_scope, 389, bind_type="simple")
	resp = reconnect(client_for, admin, [jdoe], simple)
	assert resp.status_code == 422 and "has no service account" in resp.json["message"]
	off = add_server(session_scope, 389, active=False)
	resp = reconnect(client_for, admin, [jdoe], off)
	assert resp.status_code == 422 and "is switched off" in resp.json["message"]
	assert account(session_scope, jdoe)[0] is None


def test_only_accounts_without_a_directory_are_reconnected(admin, make_user, client_for,
                                                           session_scope):
	"""A local account, and a directory account whose server is still there:
	refused, named."""
	srv = add_server(session_scope, 389)
	local = make_user(username="local1")
	linked = make_user(username="linked1", auth_type="ldap", ldap_server_id=srv)
	resp = reconnect(client_for, admin, [local.id, linked.id], srv)
	assert resp.status_code == 422
	assert "can be reconnected - not linked1, local1." in resp.json["message"]


@pytest.mark.ldap
def test_the_role_comes_from_the_group_rules_where_the_server_sets_them(
		admin, ldap_directory, admins_group, make_user, client_for, session_scope):
	"""Where the server sets roles (a service account, active rules): alice
	(in netadmins, mapped to admin) becomes an admin and bsmith (netops,
	operator) an operator, whatever they were. Where it doesn't (no rule),
	the roles are kept."""
	rules = [(ADMINS_DN, "admin"), (LDAP_GROUP_DN, "operator")]
	srv = add_server(session_scope, ldap_directory, rules=rules)
	alice = add_orphan(session_scope, "alice", role="operator")
	bsmith = add_orphan(session_scope, "bsmith", role="admin")
	resp = reconnect(client_for, admin, [alice, bsmith], srv)
	assert resp.json["admins"] == ["alice"] and resp.json["sets_roles"] is True
	assert account(session_scope, alice)[2] == "admin"
	assert account(session_scope, bsmith)[2] == "operator"
	plain = add_server(session_scope, ldap_directory)
	jdoe = add_orphan(session_scope, "jdoe", role="admin")
	resp = reconnect(client_for, admin, [jdoe], plain)
	assert resp.json["sets_roles"] is False and account(session_scope, jdoe)[2] == "admin"


@pytest.mark.ldap
def test_an_email_another_account_uses_is_left_as_it_was(admin, ldap_directory, make_user,
                                                        client_for, session_scope):
	"""The directory's email for jdoe is another account's: jdoe keeps its
	own (emails are unique), and still takes the directory's name."""
	with session_scope() as s:
		s.add(User(username="other1", email="jdoe@corp.test", is_approved=True, is_active=True))
	srv = add_server(session_scope, ldap_directory)
	jdoe = add_orphan(session_scope, "jdoe")
	assert reconnect(client_for, admin, [jdoe], srv).status_code == 200
	assert account(session_scope, jdoe)[3:] == ("jdoe@test.local", "John Doe")


@pytest.mark.parametrize("action", ["enable", "approve", "promote"])
def test_an_account_without_a_directory_cant_be_given_access(action, admin, make_user,
                                                             client_for, session_scope):
	"""Enable, Approve and Promote are refused on it - one or in bulk - with
	the way out in words; it stays disabled."""
	jdoe = add_orphan(session_scope, "jdoe")
	client = client_for(admin)
	client.post(f"/admin/users/{jdoe}/{action}")
	client.post(f"/admin/users/bulk/{action}", data={"user_ids": str(jdoe)})
	with client.session_transaction() as s:
		messages = [m for _, m in s.pop("_flashes", [])]
	assert messages == ["jdoe has no directory and can't sign in: reconnect it to an LDAP "
	                    "server first (User Management -> Reconnect)."] * 2
	assert account(session_scope, jdoe)[:3] == (None, False, "operator")


@pytest.mark.ldap
def test_the_import_reconnects_only_when_asked(admin, ldap_directory, make_user, client_for,
                                               session_scope):
	"""jdoe, kept without a directory, is picked in the directory browser:
	skipped as before without the option, reconnected (disabled, the
	directory's details) with it. A local account with a directory name
	(alice) is skipped either way, untouched."""
	srv = add_server(session_scope, ldap_directory)
	jdoe = add_orphan(session_scope, "jdoe")
	alice = make_user(username="alice").id
	picked = [{"type": "user", "username": "jdoe"}, {"type": "user", "username": "alice"}]
	client = client_for(admin)
	resp = client.post(f"/admin/server/ldap/{srv}/import", json=picked)
	assert (resp.json["reconnected"], resp.json["skipped"]) == (0, 2)
	assert account(session_scope, jdoe)[0] is None
	resp = client.post(f"/admin/server/ldap/{srv}/import", json={"items": picked, "reconnect": True})
	assert (resp.json["users_created"], resp.json["reconnected"], resp.json["skipped"]) == (0, 1, 1)
	assert account(session_scope, jdoe) == (srv, False, "operator", "jdoe@corp.test", "John Doe")
	with session_scope() as s:
		assert s.get(User, alice).auth_type == "local"
		detail = s.query(AuditLog).filter_by(action="admin.ldap_import").order_by(
			AuditLog.timestamp.desc()).first().detail
	assert detail["reconnected"] == ["jdoe"]
