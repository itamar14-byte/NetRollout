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
		assert (u.role, u.is_approved, u.is_active) == ("operator", False, False)
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
		                role="operator", ldap_server_id=ldap_server))
	with patch("src.webapp.blueprints.auth.check_group_membership",
	           return_value=("cn=netops,dc=corp", "operator")), \
			patch("src.webapp.blueprints.auth.fetch_user_details",
			      return_value={"email": "jd@corp", "full_name": "J D"}):
		resp = login(client_for(), "jdoe", "directory-pass")
	assert resp.headers["Location"] == "/dashboard"
	with session_scope() as s:
		u = s.query(User).filter_by(username="jdoe").one()
		assert (u.auth_type, u.role, u.is_approved) == ("ldap", "operator", True)


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


# ── Back to the page asked for (?next=) ──────────────────────────────────────

@pytest.mark.parametrize("value, kept", [
	("/results?job=abc", True),
	("/grafana/", True),
	("/inventory/", True),
	("//evil.example/login", False),          # protocol-relative: another site
	("///evil.example", False),               # no host for Python, one for browsers
	("https://evil.example/", False),
	("/\evil.example", False),               # browsers read \ as /
	("javascript:alert(1)", False),
	(" /results", False),
	("/res\nults", False),
	("/logout", False),                       # signing straight back out
	("/", False),
	("", False),
	(None, False),
])
def test_only_local_paths_are_returned_to(value, kept):
	from src.webapp.blueprints.auth import safe_next
	assert safe_next(value) == (value if kept else None)


def asked_for(client, path):
	# what Flask-Login / nginx do: the sign-in page with ?next=
	client.get("/", query_string={"next": path})


def test_sign_in_returns_to_the_page_asked_for_through_2fa(client_for,
                                                            make_user):
	user = make_user()
	client = client_for()
	asked_for(client, "/results?job=abc")
	assert login(client, user.username).headers["Location"] == "/otp_enroll"
	client.get("/otp_enroll")
	with client.session_transaction() as s:
		secret = s["pending_totp_secret"]
	resp = client.post("/otp_enroll", data={"code": pyotp.TOTP(secret).now()})
	assert resp.headers["Location"] == "/results?job=abc"
	# used once: the next sign-in (2FA verify) lands on the Dashboard
	client.get("/logout")
	login(client, user.username)
	resp = client.post("/otp_verify", data={"code": pyotp.TOTP(secret).now()})
	assert resp.headers["Location"] == "/dashboard"
	# and through 2FA verify too
	client.get("/logout")
	asked_for(client, "/inventory/")
	login(client, user.username)
	resp = client.post("/otp_verify", data={"code": pyotp.TOTP(secret).now()})
	assert resp.headers["Location"] == "/inventory/"


def test_a_crafted_next_lands_on_the_dashboard(client_for, make_user):
	make_user(username="admin", role="admin")          # no 2FA step
	client = client_for()
	asked_for(client, "https://evil.example/login")
	assert login(client, "admin").headers["Location"] == "/dashboard"


def test_a_forced_password_change_comes_first_then_the_page(client_for,
                                                             make_user):
	make_user(username="admin", role="admin", must_change_password=True)
	client = client_for()
	asked_for(client, "/grafana/")
	assert login(client, "admin").headers["Location"] == "/dashboard"
	assert client.get("/dashboard").headers["Location"] == "/account/password"
	resp = client.post("/account/password", data={
		"current_password": TEST_PASSWORD, "new_password": "Brand-new-pass-7",
		"confirm_password": "Brand-new-pass-7"})
	assert resp.headers["Location"] == "/grafana/"


# ── Sign-out after inactivity / after 12 hours ──────────────────────────────

import time as _time

from src.webapp import extensions as _ext


@pytest.fixture
def fresh_idle_limit():
	_ext._IDLE_CACHE.update(at=0.0, seconds=None)     # re-read the setting
	yield
	_ext._IDLE_CACHE.update(at=0.0, seconds=None)


def stamp(client, *, idle_ago=0, signed_in_ago=0):
	now = _time.time()
	with client.session_transaction() as s:
		s[_ext.LAST_ACTIVE] = now - idle_ago
		s[_ext.SIGNED_IN_AT] = now - signed_in_ago


def last_active(client):
	with client.session_transaction() as s:
		return s.get(_ext.LAST_ACTIVE)


def test_inactivity_signs_out_and_comes_back_after_signing_in(
		client_for, make_user, session_scope, fresh_idle_limit):
	user = make_user()
	client = client_for(user)
	stamp(client, idle_ago=16 * 60)                 # default limit: 15 min
	resp = client.get("/results?job=1")
	assert resp.status_code == 302
	assert resp.headers["Location"] == "/?next=/results?job%3D1"
	assert client.get("/dashboard").headers["Location"].startswith("/?next=")
	assert ("auth.session_expired", True, "idle") in audit_actions(
		session_scope, user.username)


def test_activity_keeps_the_session_and_background_does_not(
		client_for, make_user, fresh_idle_limit):
	client = client_for(make_user())
	stamp(client, idle_ago=14 * 60)
	before = last_active(client)
	for background in ({"headers": {"X-NR-Background": "1"}},
	                   {"query_string": {"_bg": "1"}}):
		assert client.get("/active_jobs", **background).status_code == 200
		assert last_active(client) == before          # still idle
	assert client.get("/dashboard").status_code == 200
	assert last_active(client) > before + 13 * 60     # a person: extended


def test_background_requests_still_expire(client_for, make_user,
                                          fresh_idle_limit):
	client = client_for(make_user())
	stamp(client, idle_ago=16 * 60)
	resp = client.get("/account/session", headers={"X-NR-Background": "1"})
	assert resp.status_code == 401 and resp.json["redirect"] == "/"


def test_twelve_hours_is_the_limit_however_active(client_for, make_user,
                                                  session_scope,
                                                  fresh_idle_limit):
	user = make_user()
	client = client_for(user)
	stamp(client, idle_ago=0, signed_in_ago=12 * 3600 + 1)
	resp = client.get("/dashboard")
	assert resp.status_code == 302 and resp.headers["Location"].startswith("/?next=")
	with session_scope() as s:
		(detail,) = [a.detail for a in s.query(AuditLog).filter_by(
			actor_username=user.username, action="auth.session_expired")]
	assert detail == {"reason": "absolute"}


def test_grafana_gets_a_bare_401_when_the_session_has_expired(
		client_for, make_user, fresh_idle_limit):
	client = client_for(make_user(role="admin"))
	stamp(client, idle_ago=16 * 60)
	resp = client.get("/_netrollout/grafana-auth")
	assert resp.status_code == 401 and not resp.data   # nginx: a status only


def test_the_limit_follows_system_settings(app, client_for, make_user,
                                           fresh_idle_limit):
	app.backend.settings.update({"session_idle_minutes": 5}, None)
	try:
		client = client_for(make_user())
		stamp(client, idle_ago=6 * 60)
		assert client.get("/dashboard").status_code == 302
	finally:
		app.backend.settings.update({"session_idle_minutes": 15}, None)


def test_the_page_can_ask_how_long_is_left(client_for, make_user,
                                           fresh_idle_limit):
	client = client_for(make_user())
	stamp(client, idle_ago=60, signed_in_ago=3600)
	body = client.get("/account/session",
	                  headers={"X-NR-Background": "1"}).json
	assert 13 * 60 <= body["idle_seconds_left"] <= 14 * 60
	assert 10 * 3600 <= body["absolute_seconds_left"] <= 11 * 3600
	# "Stay signed in" asks without the background header: it extends
	assert client.get("/account/session").json["idle_seconds_left"] >= 15 * 60 - 2


def test_signing_in_starts_both_clocks(client_for, make_user):
	make_user(username="admin", role="admin")          # no 2FA step
	client = client_for()
	login(client, "admin")
	with client.session_transaction() as s:
		assert _time.time() - s[_ext.SIGNED_IN_AT] < 5
		assert _time.time() - s[_ext.LAST_ACTIVE] < 5


@pytest.mark.parametrize("minutes, ok", [(4, False), (5, True), (480, True),
                                         (481, False)])
def test_the_setting_range(minutes, ok):
	from src.db.settings import SETTINGS
	s = SETTINGS["session_idle_minutes"]
	if ok:
		assert s.parse(minutes) == minutes
	else:
		with pytest.raises(ValueError):
			s.parse(minutes)
