"""The factory admin's optional 2FA: off by default (the break-glass
account), turned on from My Account (the QR page, a code), then asked at
every sign-in; turned off with the password and a current code. Every other
account keeps its rule: local accounts always use 2FA, LDAP ones never."""
import pyotp
import pytest

from src.accounts.users import needs_second_factor
from src.db.tables import AuditLog, User
from src.encryption import decrypt, encrypt
from tests.integration.conftest import TEST_PASSWORD

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

SECRET = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def factory(make_user):
	return make_user(username="admin", role="admin")


def login(client, username="admin", password=TEST_PASSWORD):
	return client.post("/login", data={"username": username, "password": password})


def flashes(client):
	with client.session_transaction() as s:
		return [m for _, m in s.pop("_flashes", [])]


def audited(session_scope, action):
	with session_scope() as s:
		return [(a.success, (a.detail or {}).get("reason"))
		        for a in s.query(AuditLog).filter_by(action=action).order_by(AuditLog.timestamp)]


def with_2fa(session_scope, user):
	with session_scope() as s:
		s.get(User, user.id).otp_secret = encrypt(SECRET)


def test_the_account_page_offers_2fa_to_the_factory_admin_only(factory, make_user,
                                                                client_for):
	"""The factory admin's Account page: 2FA off and "Set up two-factor
	sign-in"; an operator's has no such button."""
	page = client_for(factory).get("/account").get_data(as_text=True)
	assert "Off - this account signs in with its password alone" in page
	assert "Set up two-factor sign-in" in page
	other = client_for(make_user()).get("/account").get_data(as_text=True)
	assert "Set up two-factor sign-in" not in other
	assert "Turn off two-factor sign-in" not in other


def test_turning_2fa_on_then_signing_in_asks_the_code(factory, client_for, db_get,
                                                      session_scope):
	"""The QR page keeps a secret in the session; a code from it stores the
	secret (encrypted, audited auth.2fa_enabled). The next sign-in goes to the
	code page, and a code signs in."""
	client = client_for(factory)
	page = client.get("/account/2fa/setup")
	assert page.status_code == 200
	assert "Turn on two-factor sign-in" in page.get_data(as_text=True)
	with client.session_transaction() as s:
		secret = s["account_totp_secret"]
	resp = client.post("/account/2fa/setup", data={"code": pyotp.TOTP(secret).now()})
	assert resp.headers["Location"] == "/account"
	assert decrypt(db_get(User, factory.id).otp_secret) == secret
	assert audited(session_scope, "auth.2fa_enabled") == [(True, None)]
	assert "Turn off two-factor sign-in" in client.get("/account").get_data(as_text=True)

	fresh = client_for()
	assert login(fresh).headers["Location"] == "/otp_verify"
	resp = fresh.post("/otp_verify", data={"code": pyotp.TOTP(secret).now()})
	assert resp.headers["Location"] == "/dashboard"


def test_a_wrong_code_leaves_2fa_off(factory, client_for, db_get, session_scope):
	"""A wrong code stores nothing, says so and is audited failed."""
	client = client_for(factory)
	client.get("/account/2fa/setup")
	resp = client.post("/account/2fa/setup", data={"code": "000000"})
	assert resp.headers["Location"] == "/account/2fa/setup"
	assert flashes(client) == ["That code isn't right - try the one your app shows now."]
	assert db_get(User, factory.id).otp_secret is None
	assert audited(session_scope, "auth.2fa_enabled") == [(False, "wrong_code")]


def test_turning_2fa_off_needs_the_password_and_a_code(factory, client_for, db_get,
                                                       session_scope):
	"""A wrong password, then a wrong code, are refused (audited failed, 2FA
	stays on); the password with a current code turns it off, and the next
	sign-in needs the password alone."""
	with_2fa(session_scope, factory)
	client = client_for(factory)
	assert "Turn off two-factor sign-in" in client.get("/account/2fa/off").get_data(as_text=True)
	code = pyotp.TOTP(SECRET).now()
	client.post("/account/2fa/off", data={"password": "Wrong-pass-1", "code": code})
	assert flashes(client) == ["The password is incorrect."]
	client.post("/account/2fa/off", data={"password": TEST_PASSWORD, "code": "000000"})
	assert flashes(client) == ["That code isn't right - try the one your app shows now."]
	assert db_get(User, factory.id).otp_secret is not None
	resp = client.post("/account/2fa/off", data={"password": TEST_PASSWORD, "code": code})
	assert resp.headers["Location"] == "/account"
	assert db_get(User, factory.id).otp_secret is None
	assert audited(session_scope, "auth.2fa_disabled") == [
		(False, "wrong_password"), (False, "wrong_code"), (True, None)]
	assert login(client_for()).headers["Location"] == "/dashboard"


def test_other_accounts_cant_use_the_optional_2fa_pages(make_user, client_for,
                                                        db_get, session_scope):
	"""An operator (2FA required) is sent back to My Account from both pages,
	nothing changed."""
	user = make_user()
	with_2fa(session_scope, user)
	client = client_for(user)
	assert client.get("/account/2fa/setup").headers["Location"] == "/account"
	resp = client.post("/account/2fa/off",
	                   data={"password": TEST_PASSWORD, "code": pyotp.TOTP(SECRET).now()})
	assert resp.headers["Location"] == "/account"
	assert db_get(User, user.id).otp_secret is not None


def test_needs_second_factor_is_the_one_rule(factory, make_user, db_get, session_scope):
	"""Local accounts always; the factory admin only with an authenticator;
	LDAP accounts never."""
	assert needs_second_factor(db_get(User, make_user().id)) is True
	assert needs_second_factor(db_get(User, factory.id)) is False
	with_2fa(session_scope, factory)
	assert needs_second_factor(db_get(User, factory.id)) is True
	directory = make_user(username="ldapper", auth_type="ldap")
	assert needs_second_factor(db_get(User, directory.id)) is False
