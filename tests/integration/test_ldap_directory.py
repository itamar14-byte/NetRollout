"""LDAP against a real (ephemeral) OpenLDAP directory: search-then-bind for
nested users, escaping, credential vs availability errors, group membership,
and the login flow end to end."""
import os
import time
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
from src import ldap_auth
from src.db.tables import LDAPGroup, LDAPServer, User, AuditLog
from src.ldap_auth import LdapUnavailable
from tests.integration.conftest import (LDAP_BASE, LDAP_GROUP_DN, LDAP_USERS,
                                        LDAP_SERVICE_DN, LDAP_SERVICE_PW)

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
	assert ldap_auth.authenticate(ldap_server_config, "jdoe", JDOE_PW) == JDOE_DN


def test_constructed_dn_cannot_reach_nested_users(ldap_server_config):
	"""Why search-then-bind: without a service account the DN is guessed as
	uid=jdoe,<base_dn>, which isn't where jdoe lives, so the sign-in fails
	(None)."""
	no_service = with_(ldap_server_config, bind_type="simple")
	assert ldap_auth.constructed_dn(no_service, "jdoe") == f"uid=jdoe,{LDAP_BASE}"
	assert ldap_auth.authenticate(no_service, "jdoe", JDOE_PW) is None


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
	assert ldap_auth.authenticate(ldap_server_config, username, password) is None


def test_user_bind_wrapper(ldap_server_config):
	"""user_bind is True for the right password and False for a wrong one."""
	assert ldap_auth.user_bind(ldap_server_config, "alice", ALICE_PW) is True
	assert ldap_auth.user_bind(ldap_server_config, "alice", "nope") is False


# ── Availability errors are distinct from bad credentials ────────────────────

def test_wrong_service_account_password_is_unavailable(ldap_server_config):
	"""A wrong service account password raises LdapUnavailable, not a refusal of
	the user."""
	broken = with_(ldap_server_config,
	               bind_password=enc.encrypt("not-the-svc-password"))
	with pytest.raises(LdapUnavailable):
		ldap_auth.authenticate(broken, "jdoe", JDOE_PW)


def test_directory_down_is_unavailable_and_fast(ldap_server_config):
	"""A directory nobody listens on raises LdapUnavailable within
	CONNECT_TIMEOUT + 3 seconds."""
	down = with_(ldap_server_config, port=1)  # nothing listens here
	start = time.monotonic()
	with pytest.raises(LdapUnavailable):
		ldap_auth.authenticate(down, "jdoe", JDOE_PW)
	assert time.monotonic() - start < ldap_auth.CONNECT_TIMEOUT + 3


# ── Group membership ─────────────────────────────────────────────────────────

def test_group_member_matches(ldap_server_config):
	"""A member of the mapped group matches it, with the group's role."""
	assert ldap_auth.check_group_membership(
		ldap_server_config, "jdoe", JDOE_PW, GROUPS) == (LDAP_GROUP_DN, "operator")


def test_member_dn_with_comma_matches(ldap_server_config):
	"""A member whose DN holds an escaped comma ("cn=Smith\\, Bob") matches: it
	must be escaped inside the (member=...) filter."""
	assert ldap_auth.check_group_membership(
		ldap_server_config, "bsmith", BOB_PW, GROUPS) == (LDAP_GROUP_DN, "operator")


def test_non_member_and_bad_password_do_not_match(ldap_server_config):
	"""A user outside the group, and a member with a wrong password, match no
	group (None)."""
	assert ldap_auth.check_group_membership(
		ldap_server_config, "alice", ALICE_PW, GROUPS) is None
	assert ldap_auth.check_group_membership(
		ldap_server_config, "jdoe", "wrong", GROUPS) is None


# ── Admin tools ──────────────────────────────────────────────────────────────

def test_user_details_and_tree(ldap_server_config):
	"""fetch_user_details returns jdoe's email and full name and None for `*`;
	walk_tree under ou=Groups lists the one netops group."""
	details = ldap_auth.fetch_user_details(ldap_server_config, "jdoe")
	assert details == {"email": "jdoe@corp.test", "full_name": "John Doe"}
	assert ldap_auth.fetch_user_details(ldap_server_config, "*") is None
	tree = ldap_auth.walk_tree(ldap_server_config, f"ou=Groups,{LDAP_BASE}")
	assert tree["entries"] == [{"type": "group", "dn": LDAP_GROUP_DN,
	                            "label": "netops", "username": None}]


def test_connection_and_user_test_tools(ldap_server_config):
	"""The admin's connection and user tests report ok against the directory
	and error when it's down."""
	assert ldap_auth.test_connection(ldap_server_config)["status"] == "ok"
	assert ldap_auth.test_user(ldap_server_config, "jdoe", JDOE_PW)["status"] == "ok"
	down = with_(ldap_server_config, port=1)
	assert ldap_auth.test_connection(down)["status"] == "error"
	assert ldap_auth.test_user(down, "jdoe", JDOE_PW)["status"] == "error"


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
