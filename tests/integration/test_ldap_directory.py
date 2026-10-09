"""LDAP against a real (ephemeral) OpenLDAP directory: search-then-bind for
nested users, escaping, credential vs availability errors, group membership,
and the login flow end to end."""
import os
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from ldap3 import Connection, Server
from ldap3.core.exceptions import LDAPException

import src.encryption as enc
from src.accounts import ldap
from src.accounts.ldap import LdapUnavailable
from src.db.tables import LDAPGroup, LDAPServer, User, AuditLog
from tests.integration.conftest import (LDAP_ADMIN_DN, LDAP_ADMIN_PW, LDAP_BASE,
                                        LDAP_GROUP_DN, LDAP_USERS, LDAP_SERVICE_DN,
                                        LDAP_SERVICE_PW)

pytestmark = pytest.mark.ldap

JDOE_DN, JDOE_PW = LDAP_USERS["jdoe"]
BOB_DN, BOB_PW = LDAP_USERS["bsmith"]
ALICE_DN, ALICE_PW = LDAP_USERS["alice"]
GROUPS = [SimpleNamespace(group_dn=LDAP_GROUP_DN, role="operator")]


@pytest.fixture(autouse=True)
def cipher(request):
	"""An encryption key for the tests that don't use the app fixture."""
	# The app fixture (e2e tests) initialises its own key; pure directory
	# tests need one for the encrypted service-account password
	if "app" not in request.fixturenames:
		os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
		enc.init_encryption(None)


def with_(cfg, **changes):
	"""A copy of the server config with some fields changed."""
	return SimpleNamespace(**{**vars(cfg), **changes})


# ── Search-then-bind ─────────────────────────────────────────────────────────

def test_nested_user_resolves_to_real_dn(ldap_server_config):
	"""A user in a nested OU signs in and resolves to the real DN found by search."""
	assert ldap.authenticate(ldap_server_config, "jdoe", JDOE_PW) == JDOE_DN


def test_constructed_dn_cannot_reach_nested_users(ldap_server_config):
	"""Why search-then-bind: without a service account the DN is guessed as
	uid=jdoe,<base_dn>, which isn't where jdoe lives, so the sign-in fails
	(None)."""
	no_service = with_(ldap_server_config, bind_type="simple")
	assert ldap.constructed_dn(no_service, "jdoe") == f"uid=jdoe,{LDAP_BASE}"
	assert ldap.authenticate(no_service, "jdoe", JDOE_PW) is None


@pytest.mark.parametrize("username,password", [
	("jdoe", "wrong-pass"),          # wrong password
	("nobody", "x"),                 # unknown user
	("jdoe", ""),                    # empty password: never sent
	("*", JDOE_PW),                  # wildcard as username
	("jdoe)(uid=*", JDOE_PW),        # filter injection
	("dup", "dup-pass"),             # ambiguous: two entries share the uid
])
def test_rejected_logins_return_none_not_errors(ldap_server_config, username,
                                                password):
	"""A wrong password, unknown user, empty password, wildcard or injected
	filter as username, or a uid shared by two entries returns None, never
	raises."""
	assert ldap.authenticate(ldap_server_config, username, password) is None


def test_user_bind_wrapper(ldap_server_config):
	"""user_bind is True for the right password and False for a wrong one."""
	assert ldap.user_bind(ldap_server_config, "alice", ALICE_PW) is True
	assert ldap.user_bind(ldap_server_config, "alice", "nope") is False


# ── Availability errors are distinct from bad credentials ────────────────────

def test_wrong_service_account_password_is_unavailable(ldap_server_config):
	"""A wrong service account password raises LdapUnavailable, not a refusal of
	the user."""
	broken = with_(ldap_server_config,
	               bind_password=enc.encrypt("not-the-svc-password"))
	with pytest.raises(LdapUnavailable):
		ldap.authenticate(broken, "jdoe", JDOE_PW)


def test_directory_down_is_unavailable_and_fast(ldap_server_config):
	"""A directory nobody listens on raises LdapUnavailable within
	CONNECT_TIMEOUT + 3 seconds."""
	down = with_(ldap_server_config, port=1)  # nothing listens here
	start = time.monotonic()
	with pytest.raises(LdapUnavailable):
		ldap.authenticate(down, "jdoe", JDOE_PW)
	assert time.monotonic() - start < ldap.CONNECT_TIMEOUT + 3


# ── Group membership ─────────────────────────────────────────────────────────

def test_group_member_matches(ldap_server_config):
	"""A member of the mapped group matches it, with the group's role."""
	assert ldap.check_group_membership(
		ldap_server_config, "jdoe", JDOE_PW, GROUPS) == (LDAP_GROUP_DN, "operator")


def test_member_dn_with_comma_matches(ldap_server_config):
	"""A member whose DN holds an escaped comma ("cn=Smith\\, Bob") matches: it
	must be escaped inside the (member=...) filter."""
	assert ldap.check_group_membership(
		ldap_server_config, "bsmith", BOB_PW, GROUPS) == (LDAP_GROUP_DN, "operator")


def test_non_member_and_bad_password_do_not_match(ldap_server_config):
	"""A user outside the group, and a member with a wrong password, match no
	group (None)."""
	assert ldap.check_group_membership(
		ldap_server_config, "alice", ALICE_PW, GROUPS) is None
	assert ldap.check_group_membership(
		ldap_server_config, "jdoe", "wrong", GROUPS) is None


# ── Admin tools ──────────────────────────────────────────────────────────────

def test_user_details_and_tree(ldap_server_config):
	"""fetch_user_details returns jdoe's email and full name and None for `*`;
	walk_tree under ou=Groups lists the one netops group."""
	details = ldap.fetch_user_details(ldap_server_config, "jdoe")
	assert details == {"email": "jdoe@corp.test", "full_name": "John Doe"}
	assert ldap.fetch_user_details(ldap_server_config, "*") is None
	tree = ldap.walk_tree(ldap_server_config, f"ou=Groups,{LDAP_BASE}")
	assert tree["entries"] == [{"type": "group", "dn": LDAP_GROUP_DN,
	                            "label": "netops", "username": None}]


def test_connection_and_user_test_tools(ldap_server_config):
	"""The admin's connection and user tests report ok against the directory
	and error when it's down."""
	assert ldap.test_connection(ldap_server_config)["status"] == "ok"
	assert ldap.test_user(ldap_server_config, "jdoe", JDOE_PW)["status"] == "ok"
	down = with_(ldap_server_config, port=1)
	assert ldap.test_connection(down)["status"] == "error"
	assert ldap.test_user(down, "jdoe", JDOE_PW)["status"] == "error"


def test_simple_bind_type_tools(ldap_server_config):
	"""Without a service account (bind type simple) the connection test only opens
	the connection: ok "Connection established", error when the directory is
	down; the user test of a user found at the constructed DN fails with "User
	<name> failed" (jdoe isn't directly under the base DN)."""
	simple = with_(ldap_server_config, bind_type="simple")
	assert ldap.test_connection(simple) == {"status": "ok",
	                                        "message": "Connection established"}
	assert ldap.test_connection(with_(simple, port=1))["status"] == "error"
	assert ldap.test_user(simple, "jdoe", JDOE_PW) == {"status": "error",
	                                                   "message": "User jdoe failed"}


def test_user_test_answers(ldap_server_config):
	"""The admin's user test: "User <name> connected" for the right password,
	"User <name> failed" for a wrong one."""
	assert ldap.test_user(ldap_server_config, "jdoe", JDOE_PW) == \
	       {"status": "ok", "message": "User jdoe connected"}
	assert ldap.test_user(ldap_server_config, "jdoe", "wrong") == \
	       {"status": "error", "message": "User jdoe failed"}


def test_base_dn_is_the_directorys_naming_context(ldap_server_config):
	"""OpenLDAP has no defaultNamingContext: the base DN is its naming context;
	a directory that's down is an error."""
	assert ldap.fetch_base_dn(ldap_server_config) == {"status": "ok",
	                                                  "base_dn": LDAP_BASE}
	assert ldap.fetch_base_dn(with_(ldap_server_config, port=1))["status"] == "error"


def test_user_details_fall_back_to_the_common_name(ldap_server_config):
	"""A user without mail or displayName: no email, the cn as the full name."""
	assert ldap.fetch_user_details(ldap_server_config, "alice") == \
	       {"email": None, "full_name": "Alice Brown"}
	assert ldap.fetch_user_details(ldap_server_config, "nobody") is None


def test_tree_at_the_base_lists_the_ous(ldap_server_config):
	"""walk_tree with no DN looks at the base DN: its organizational units, each
	type "ou" with no username."""
	tree = ldap.walk_tree(ldap_server_config)
	assert tree["status"] == "ok"
	assert sorted((e["type"], e["dn"], e["username"]) for e in tree["entries"]) == [
		("ou", f"ou={ou},{LDAP_BASE}", None) for ou in ("Groups", "Service", "Users")]


def test_ous_are_labelled_by_their_name(ldap_server_config):
	"""An OU (which has no cn) is labelled by its ou value - at the base and
	nested - never by an empty attribute list ("[]")."""
	base = ldap.walk_tree(ldap_server_config)["entries"]
	assert sorted(e["label"] for e in base) == ["Groups", "Service", "Users"]
	nested = ldap.walk_tree(ldap_server_config, f"ou=Users,{LDAP_BASE}")["entries"]
	assert [e for e in nested if e["type"] == "ou"] == [
		{"type": "ou", "dn": f"ou=Network,ou=Users,{LDAP_BASE}", "label": "Network",
		 "username": None}]


BROWSE_OU = f"ou=Browse,{LDAP_BASE}"
PERSON_CLASSES = ["top", "person", "organizationalPerson", "inetOrgPerson"]


@pytest.fixture
def browse_entries(ldap_directory):
	"""An OU of entries for the tree browser, added as the directory's admin and
	removed afterwards: a person with a uid, a person without one, a group, and
	a device (neither person nor group)."""
	conn = Connection(Server("127.0.0.1", port=ldap_directory), LDAP_ADMIN_DN,
	                  LDAP_ADMIN_PW, auto_bind=True, raise_exceptions=True)
	entries = [
		(BROWSE_OU, ["organizationalUnit"], {"ou": "Browse"}),
		(f"cn=Carol White,{BROWSE_OU}", PERSON_CLASSES,
		 {"cn": "Carol White", "sn": "White", "uid": "cwhite"}),
		(f"cn=Robot,{BROWSE_OU}", ["top", "person"], {"cn": "Robot", "sn": "Robot"}),
		(f"cn=browsers,{BROWSE_OU}", ["groupOfNames"],
		 {"cn": "browsers", "member": f"cn=Robot,{BROWSE_OU}"}),
		(f"cn=printer,{BROWSE_OU}", ["device"], {"cn": "printer"}),
	]
	try:
		for dn, classes, attrs in entries:
			conn.add(dn, classes, attrs)
		yield
	finally:
		for dn, _, _ in reversed(entries):
			try:
				conn.delete(dn)
			except LDAPException:
				pass                 # not added: nothing to remove
		conn.unbind()


def test_tree_lists_people_groups_and_ous(ldap_server_config, browse_entries):
	"""One level of the tree: a person is type "user" labelled by cn with the
	cn_identifier (uid) as username, falling back to the cn without one; a
	groupOfNames is type "group" with no username; an entry that is neither
	(a device) isn't listed."""
	tree = ldap.walk_tree(ldap_server_config, BROWSE_OU)
	assert tree["status"] == "ok"
	assert sorted(tree["entries"], key=lambda e: e["dn"]) == [
		{"type": "user", "dn": f"cn=Carol White,{BROWSE_OU}", "label": "Carol White",
		 "username": "cwhite"},
		{"type": "user", "dn": f"cn=Robot,{BROWSE_OU}", "label": "Robot",
		 "username": "Robot"},
		{"type": "group", "dn": f"cn=browsers,{BROWSE_OU}", "label": "browsers",
		 "username": None},
	]


PEOPLE_OU = f"ou=People,{LDAP_BASE}"


@pytest.fixture
def people_entries(ldap_directory):
	"""An OU of users as directories store them, added as the directory's admin
	and removed afterwards: an organizationalPerson only (no uid), and an RFC
	2307 Unix account (account + posixAccount, no person class)."""
	conn = Connection(Server("127.0.0.1", port=ldap_directory), LDAP_ADMIN_DN,
	                  LDAP_ADMIN_PW, auto_bind=True, raise_exceptions=True)
	entries = [
		(PEOPLE_OU, ["organizationalUnit"], {"ou": "People"}),
		(f"cn=Dan Grey,{PEOPLE_OU}", ["organizationalPerson"],
		 {"cn": "Dan Grey", "sn": "Grey"}),
		(f"uid=eve,{PEOPLE_OU}", ["account", "posixAccount"],
		 {"uid": "eve", "cn": "Eve Black", "uidNumber": "1001",
		  "gidNumber": "1001", "homeDirectory": "/home/eve"}),
	]
	try:
		for dn, classes, attrs in entries:
			conn.add(dn, classes, attrs)
		yield
	finally:
		for dn, _, _ in reversed(entries):
			try:
				conn.delete(dn)
			except LDAPException:
				pass                 # not added: nothing to remove
		conn.unbind()


def test_tree_lists_users_by_their_stored_classes(ldap_server_config,
                                                  people_entries):
	"""OpenLDAP returns only the object classes an entry was stored with (not
	their superclasses): inetOrgPerson-only, organizationalPerson-only and
	posixAccount users are all listed as type "user" - labelled by cn, the
	uid as username (the cn without one)."""
	users = [e for e in ldap.walk_tree(ldap_server_config, f"ou=Users,{LDAP_BASE}")
	         ["entries"] if e["type"] == "user"]
	assert sorted(users, key=lambda e: e["dn"]) == [
		{"type": "user", "dn": ALICE_DN, "label": "Alice Brown", "username": "alice"},
		{"type": "user", "dn": f"cn=Dup One,ou=Users,{LDAP_BASE}", "label": "Dup One",
		 "username": "dup"},
		# OpenLDAP returns the escaped comma in its hex form
		{"type": "user", "dn": rf"cn=Smith\2C Bob,ou=Users,{LDAP_BASE}",
		 "label": "Smith, Bob", "username": "bsmith"},
	]
	people = ldap.walk_tree(ldap_server_config, PEOPLE_OU)["entries"]
	assert sorted(people, key=lambda e: e["dn"]) == [
		{"type": "user", "dn": f"cn=Dan Grey,{PEOPLE_OU}", "label": "Dan Grey",
		 "username": "Dan Grey"},
		{"type": "user", "dn": f"uid=eve,{PEOPLE_OU}", "label": "Eve Black",
		 "username": "eve"},
	]


def test_tree_errors_are_reported(ldap_server_config):
	"""walk_tree reports status error (with the directory's message) for a DN that
	doesn't exist and for a service account that can't bind."""
	missing = ldap.walk_tree(ldap_server_config, f"ou=Nowhere,{LDAP_BASE}")
	assert missing["status"] == "error" and missing["message"]
	broken = with_(ldap_server_config, bind_password=enc.encrypt("not-the-svc-password"))
	answer = ldap.walk_tree(broken)
	assert answer["status"] == "error" and answer["message"]


def test_the_first_matching_group_wins(ldap_server_config):
	"""Groups are tried in order: the first mapped group the user is a member of
	gives the role (a later mapping of the same group isn't looked at); no group
	mapped -> None."""
	groups = [SimpleNamespace(group_dn=LDAP_GROUP_DN, role="admin"),
	          SimpleNamespace(group_dn=LDAP_GROUP_DN, role="operator")]
	assert ldap.check_group_membership(ldap_server_config, "jdoe", JDOE_PW, groups) == \
	       (LDAP_GROUP_DN, "admin")
	assert ldap.check_group_membership(ldap_server_config, "jdoe", JDOE_PW, []) is None


def test_a_mapped_group_that_does_not_exist_is_skipped(ldap_server_config, capsys,
                                                       monkeypatch):
	"""A mapped group whose DN isn't in the directory (deleted, renamed) is "not
	a member of it", not the directory failing: a member of a valid group
	mapped after it gets that group's role, a non-member None. The missing DN
	is reported once (ACTION NEEDED on the console), not at every sign-in."""
	monkeypatch.setattr(ldap, "_reported_missing_groups", set(), raising=False)
	gone = f"cn=gone,ou=Groups,{LDAP_BASE}"
	groups = [SimpleNamespace(group_dn=gone, role="admin"),
	          SimpleNamespace(group_dn=LDAP_GROUP_DN, role="operator")]
	assert ldap.check_group_membership(ldap_server_config, "jdoe", JDOE_PW, groups) == \
	       (LDAP_GROUP_DN, "operator")
	assert capsys.readouterr().out == (
		f"[NetRollout] ACTION NEEDED - the LDAP group {gone} mapped to role admin "
		f"doesn't exist in the directory: sign-ins skip it until the mapping is "
		f"fixed or removed\n")
	assert ldap.check_group_membership(ldap_server_config, "alice", ALICE_PW,
	                                   groups) is None
	assert capsys.readouterr().out == ""


# ── Login end to end (also needs Postgres + Redis) ───────────────────────────

@pytest.fixture
def directory_in_db(app, ldap_directory):
	"""The id of an active LDAP server row for the test directory, with its
	netops group mapped to operator."""
	with app.backend.postgres.get_session() as s:
		srv = LDAPServer(name="corp", host="127.0.0.1", port=ldap_directory,
		                 base_dn=LDAP_BASE, cn_identifier="uid",
		                 bind_type="regular", bind_dn=LDAP_SERVICE_DN,
		                 bind_password=enc.encrypt(LDAP_SERVICE_PW),
		                 is_active=True)
		s.add(srv)
		s.flush()
		s.add(LDAPGroup(group_dn=LDAP_GROUP_DN, label="netops", role="operator",
		                ldap_server_id=srv.id))
		return srv.id


def login(client, username, password):
	return client.post("/login", data={"username": username,
	                                   "password": password})


@pytest.mark.postgres
@pytest.mark.redis
def test_group_member_first_login_provisions_then_logs_in_again(
		directory_in_db, client_for, session_scope):
	"""A group member's first sign-in reaches the Dashboard and creates an LDAP
	user with the directory's email and name; the next sign-in (as a known LDAP
	user) works too, and a wrong password is sent back to the sign-in page."""
	assert login(client_for(), "jdoe", JDOE_PW).headers["Location"] == "/dashboard"
	with session_scope() as s:
		u = s.query(User).filter_by(username="jdoe").one()
		assert (u.auth_type, u.email, u.full_name) == \
		       ("ldap", "jdoe@corp.test", "John Doe")
	# second login takes the existing-LDAP-user path
	assert login(client_for(), "jdoe", JDOE_PW).headers["Location"] == "/dashboard"
	assert login(client_for(), "jdoe", "wrong").headers["Location"] == "/"


@pytest.mark.postgres
@pytest.mark.redis
def test_non_member_cannot_log_in(directory_in_db, client_for):
	"""A directory user outside the mapped group is sent back to the sign-in page."""
	assert login(client_for(), "alice", ALICE_PW).headers["Location"] == "/"


@pytest.mark.postgres
@pytest.mark.redis
def test_directory_outage_shows_unavailable_not_500(directory_in_db,
                                                    client_for, session_scope):
	"""With the directory down the sign-in redirects back (no 500), flashes
	"LDAP authentication service unavailable" and audits reason
	ldap_unavailable."""
	with session_scope() as s:
		s.get(LDAPServer, directory_in_db).port = 1  # directory "down"
	client = client_for()
	resp = login(client, "jdoe", JDOE_PW)
	assert resp.status_code == 302 and resp.headers["Location"] == "/"
	with client.session_transaction() as sess:
		# the Redis session store round-trips flashes as lists
		assert ["danger", "LDAP authentication service unavailable"] in \
		       [list(f) for f in sess["_flashes"]]
	with session_scope() as s:
		reasons = [(a.detail or {}).get("reason") for a in
		           s.query(AuditLog).filter_by(actor_username="jdoe")]
	assert "ldap_unavailable" in reasons
