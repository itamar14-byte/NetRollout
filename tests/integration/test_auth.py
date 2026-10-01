"""Authentication flows: local login + OTP, registration, gates, LDAP
(server mocked), rate limiting, logout/account."""
from unittest.mock import patch

import pyotp
import pytest

from src.db.tables import AuditLog, LDAPGroup, LDAPServer, User
from tests.integration.conftest import TEST_PASSWORD

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def login(client, username, password=TEST_PASSWORD):
	return client.post("/login", data={"username": username,
	                                   "password": password})


def pre_auth_user(client):
	with client.session_transaction() as s:
		return s.get("pre_auth_user_id")


def audit_actions(session_scope, username):
	with session_scope() as s:
		return [(a.action, a.success, (a.detail or {}).get("reason"))
		        for a in s.query(AuditLog).filter_by(actor_username=username)
		        .order_by(AuditLog.timestamp)]


# ── Local login + OTP ────────────────────────────────────────────────────────

def test_first_login_enrolls_otp_then_verifies(client_for, make_user, db_get):
	user = make_user()
	client = client_for()
	resp = login(client, user.username)
	assert resp.headers["Location"] == "/otp_enroll"
	assert pre_auth_user(client) == str(user.id)

	assert client.get("/otp_enroll").status_code == 200  # QR page
	with client.session_transaction() as s:
		secret = s["pending_totp_secret"]
	resp = client.post("/otp_enroll", data={"code": pyotp.TOTP(secret).now()})
	assert resp.headers["Location"] == "/dashboard"
	assert client.get("/dashboard").status_code == 200

	stored = db_get(User, user.id).otp_secret
	assert stored.startswith("gAAAAA") and secret not in stored  # encrypted

	# second login goes to verify, with the enrolled secret
	client.get("/logout")
	resp = login(client, user.username)
	assert resp.headers["Location"] == "/otp_verify"
	resp = client.post("/otp_verify", data={"code": pyotp.TOTP(secret).now()})
	assert resp.headers["Location"] == "/dashboard"


def test_wrong_otp_code_does_not_log_in(client_for, make_user):
	user = make_user()
	client = client_for()
	login(client, user.username)
	client.get("/otp_enroll")
	resp = client.post("/otp_enroll", data={"code": "000000"})
	assert resp.headers["Location"] == "/otp_enroll"
	assert client.get("/dashboard").status_code == 302  # still logged out


def test_otp_routes_require_password_step(client_for):
	client = client_for()
	assert client.get("/otp_enroll").headers["Location"] == "/"
	assert client.post("/otp_verify", data={"code": "123456"}) \
		       .headers["Location"] == "/"


def test_factory_admin_skips_otp(client_for, make_user):
	make_user(username="admin", role="admin")
	resp = login(client_for(), "admin")
	assert resp.headers["Location"] == "/dashboard"


@pytest.mark.parametrize("kwargs,reason", [
	({"approved": False, "active": False}, "pending_approval"),
	({"approved": True, "active": False}, "account_disabled"),
])
def test_approval_and_active_gates(client_for, make_user, session_scope,
                                   kwargs, reason):
	user = make_user(**kwargs)
	client = client_for()
	resp = login(client, user.username)
	assert resp.headers["Location"] == "/"
	assert pre_auth_user(client) is None
	assert ("auth.login", False, reason) in audit_actions(session_scope,
	                                                      user.username)


def test_wrong_password_is_audited(client_for, make_user, session_scope):
	user = make_user()
	resp = login(client_for(), user.username, "wrong")
	assert resp.headers["Location"] == "/"
	assert ("auth.login", False, "invalid_credentials") in \
	       audit_actions(session_scope, user.username)


def test_cross_origin_login_is_rejected(client_for, make_user):
	user = make_user()
	client = client_for()
	resp = client.post("/login", data={"username": user.username,
	                                   "password": TEST_PASSWORD},
	                   headers={"Origin": "https://evil.example"})
	assert resp.headers["Location"] == "/"
	assert pre_auth_user(client) is None


def test_login_is_rate_limited(client_for):
	# Replaces the old live-server test: the limiter is memory-backed, so
	# the test client exercises it without touching the running app
	client = client_for()
	codes = [client.post("/login", data={"username": "nobody",
	                                     "password": "x"}).status_code
	         for _ in range(12)]
	assert 429 not in codes[:10]
	assert codes[10] == 429


# ── Registration ─────────────────────────────────────────────────────────────

def test_registration_creates_pending_user_and_ignores_role(client_for,
                                                            session_scope):
	resp = client_for().post("/register", data={
		"username": "newbie", "password": "Str0ng-pass", "email": "n@x.io",
		"full_name": "New Bie", "role": "admin"})
	assert resp.headers["Location"] == "/"
	with session_scope() as s:
		u = s.query(User).filter_by(username="newbie").one()
		assert (u.role, u.is_approved, u.is_active) == ("user", False, False)
		assert u.password_hash != "Str0ng-pass"


def test_duplicate_registration_rejected(client_for, make_user):
	user = make_user()
	resp = client_for().post("/register", data={
		"username": user.username, "password": "Str0ng-pass", "email": "other@x.io",
		"full_name": "Dup"})
	assert resp.headers["Location"] == "/register"


# ── LDAP (directory mocked) ──────────────────────────────────────────────────

@pytest.fixture
def ldap_server(session_scope):
	with session_scope() as s:
		srv = LDAPServer(name="corp", host="ldap.test", port=389,
		                 base_dn="dc=corp", bind_type="regular",
		                 bind_dn="cn=svc", is_active=True)
		s.add(srv)
		s.flush()
		return srv.id


def test_existing_ldap_user_logs_in_without_otp(client_for, make_user,
                                                 ldap_server, session_scope):
	user = make_user()
	with session_scope() as s:
		u = s.get(User, user.id)
		u.auth_type, u.ldap_server_id, u.password_hash = "ldap", ldap_server, None
	with patch("src.webapp.blueprints.auth.user_bind", return_value=True):
		resp = login(client_for(), user.username, "directory-pass")
	assert resp.headers["Location"] == "/dashboard"


def test_ldap_bind_failure_is_rejected(client_for, make_user, ldap_server,
                                       session_scope):
	user = make_user()
	with session_scope() as s:
		u = s.get(User, user.id)
		u.auth_type, u.ldap_server_id = "ldap", ldap_server
	with patch("src.webapp.blueprints.auth.user_bind", return_value=False):
		resp = login(client_for(), user.username, "bad")
	assert resp.headers["Location"] == "/"


def test_ldap_group_member_is_auto_provisioned(client_for, ldap_server,
                                               session_scope):
	with session_scope() as s:
		s.add(LDAPGroup(group_dn="cn=netops,dc=corp", label="netops",
		                role="user", ldap_server_id=ldap_server))
	with patch("src.webapp.blueprints.auth.check_group_membership",
	           return_value=("cn=netops,dc=corp", "user")), \
			patch("src.webapp.blueprints.auth.fetch_user_details",
			      return_value={"email": "jd@corp", "full_name": "J D"}):
		resp = login(client_for(), "jdoe", "directory-pass")
	assert resp.headers["Location"] == "/dashboard"
	with session_scope() as s:
		u = s.query(User).filter_by(username="jdoe").one()
		assert (u.auth_type, u.role, u.is_approved) == ("ldap", "user", True)


def test_unknown_user_without_group_match_is_rejected(client_for, ldap_server):
	with patch("src.webapp.blueprints.auth.check_group_membership",
	           return_value=None):
		resp = login(client_for(), "stranger", "x")
	assert resp.headers["Location"] == "/"


# ── Session lifecycle ────────────────────────────────────────────────────────

def test_logout_ends_session(client_for, make_user):
	client = client_for(make_user())
	assert client.get("/dashboard").status_code == 200
	assert client.get("/logout").headers["Location"] == "/"
	assert client.get("/dashboard").status_code == 302


def test_account_page(client_for, make_user):
	assert client_for(make_user()).get("/account").status_code == 200
