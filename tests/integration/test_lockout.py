"""Account lockout: wrong passwords in a row lock a local account for 15
minutes (the 5th) and, once the lock has run out, disable it (5 more) - the
factory admin is locked again instead, never disabled; LDAP accounts are
never counted; a right password, an admin's Enable / Reset Password start
the count again. An ordinary account's page never tells: every answer is
"Invalid credentials", as for a name with no account."""
import datetime as dt
from unittest.mock import patch

import pytest

from src.accounts import users
from src.db.tables import AuditLog, LDAPServer, User
from src.webapp.hooks import conn_limit
from tests.integration.conftest import TEST_PASSWORD

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def login(client, username, password=TEST_PASSWORD):
	"""One sign-in. The per-address rate limit (10 a minute) is reset first:
	these tests are about the per-account count, and take more tries."""
	conn_limit.reset()
	return client.post("/login", data={"username": username, "password": password})


def flashes(client):
	"""The messages waiting for the next page (and taken off it)."""
	with client.session_transaction() as s:
		return [m for _, m in s.pop("_flashes", [])]


def actions(session_scope, action):
	"""The audit rows of `action`, as (actor_username, success, detail)."""
	with session_scope() as s:
		return [(a.actor_username, a.success, a.detail)
		        for a in s.query(AuditLog).filter_by(action=action)]


def wrong(client, username, times):
	"""`times` wrong passwords; the messages shown after each."""
	shown = []
	for _ in range(times):
		login(client, username, "Wrong-pass-1")
		shown.append(flashes(client))
	return shown


def expire_lock(session_scope, user_id):
	"""The account's lock ran out a second ago."""
	with session_scope() as s:
		s.get(User, user_id).locked_until = (
			dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))


def test_the_fifth_wrong_password_locks_a_local_account_for_15_minutes(
		client_for, make_user, db_get, session_scope):
	"""Four wrong passwords count; the 5th locks the account for 15 minutes
	(audited auth.account_lock_timed), it stays active. Every answer is
	"Invalid credentials" - the same as for a name with no account. While
	locked no password is checked: the right one is refused alike and a
	wrong one isn't counted."""
	user = make_user()
	client = client_for()
	shown = wrong(client, user.username, 4)
	assert db_get(User, user.id).failed_sign_ins == 4
	assert db_get(User, user.id).locked_until is None
	shown += wrong(client, user.username, 1)
	unknown = wrong(client, "nobody-has-this-name", 5)
	assert shown == unknown == [["Invalid credentials"]] * 5
	row = db_get(User, user.id)
	assert row.is_active is True and row.failed_sign_ins == 5
	left = row.locked_until - dt.datetime.now(dt.timezone.utc)
	assert dt.timedelta(minutes=14) < left <= dt.timedelta(minutes=15)
	assert actions(session_scope, "auth.account_lock_timed") == [
		(user.username, False, {"failed_passwords": 5, "minutes": 15})]
	resp = login(client, user.username)
	assert resp.headers["Location"] == "/"
	assert flashes(client) == ["Invalid credentials"]
	wrong(client, user.username, 3)
	assert db_get(User, user.id).failed_sign_ins == 5
	assert db_get(User, user.id).is_active is True


def test_five_more_after_the_lock_disable_the_account(
		client_for, make_user, db_get, session_scope):
	"""Once the lock has run out, the count goes on: four more wrong passwords
	leave the account active, the 5th more (10th in a row) disables it
	(audited auth.account_locked) - all answered "Invalid credentials". The
	right password afterwards is told the account is disabled."""
	user = make_user()
	client = client_for()
	wrong(client, user.username, 5)
	expire_lock(session_scope, user.id)
	shown = wrong(client, user.username, 4)
	row = db_get(User, user.id)
	assert row.is_active is True and row.failed_sign_ins == 9 and row.locked_until is None
	shown += wrong(client, user.username, 1)
	assert shown == [["Invalid credentials"]] * 5
	assert db_get(User, user.id).is_active is False
	assert actions(session_scope, "auth.account_locked") == [
		(user.username, False, {"failed_passwords": 10})]
	login(client, user.username)
	assert flashes(client) == ["User disabled, please check with administrator"]


def test_a_right_password_after_the_lock_starts_again(
		client_for, make_user, db_get, session_scope):
	"""After the lock has run out, the right password signs in (on to 2FA) and
	the count is 0: five more wrong passwords lock the account again, they
	don't disable it."""
	user = make_user()
	client = client_for()
	wrong(client, user.username, 5)
	expire_lock(session_scope, user.id)
	assert login(client, user.username).headers["Location"] == "/otp_enroll"
	row = db_get(User, user.id)
	assert row.failed_sign_ins == 0 and row.locked_until is None
	wrong(client_for(), user.username, 5)
	row = db_get(User, user.id)
	assert row.is_active is True and row.locked_until is not None


def test_the_users_page_shows_a_lock_that_enable_lifts(
		admin, client_for, make_user, db_get):
	"""User Management marks a locked account (a Locked badge, "locked" for the
	page's checks, so Enable is offered); the admin's Enable lifts the lock
	and the user signs in."""
	user = make_user()
	wrong(client_for(), user.username, 5)
	page = client_for(admin).get("/admin/users").get_data(as_text=True)
	(row,) = [line for line in page.splitlines() if f'"username": "{user.username}"' in line]
	assert '"locked": true' in row
	assert ">Locked</span>" in page
	client_for(admin).post(f"/admin/users/{user.id}/enable")
	assert db_get(User, user.id).locked_until is None
	assert login(client_for(), user.username).headers["Location"] == "/otp_enroll"


def test_a_right_password_starts_the_count_again(client_for, make_user, db_get):
	"""Four wrong, then the right one (the 2FA step starts): the count is 0, so
	four more wrong ones leave the account active."""
	user = make_user()
	client = client_for()
	wrong(client, user.username, 4)
	assert login(client, user.username).headers["Location"] == "/otp_enroll"
	assert db_get(User, user.id).failed_sign_ins == 0
	other = client_for()
	wrong(other, user.username, 4)
	assert db_get(User, user.id).is_active is True


def test_a_disabled_or_waiting_account_is_not_counted(client_for, make_user, db_get):
	"""Wrong passwords on an account that can't sign in anyway (disabled, or
	an access request waiting) aren't counted and audit no lockout."""
	disabled = make_user(active=False)
	waiting = make_user(approved=False, active=False)
	client = client_for()
	wrong(client, disabled.username, 6)
	wrong(client, waiting.username, 3)
	assert db_get(User, disabled.id).failed_sign_ins == 0
	assert db_get(User, waiting.id).failed_sign_ins == 0


@pytest.mark.parametrize("action", ["enable", "reset_password"])
def test_an_admins_enable_or_reset_starts_the_count_again(
		action, admin, client_for, make_user, db_get):
	"""A user locked out by wrong passwords: the admin's Enable makes the
	account active with the count at 0; a Reset Password sets the count to 0
	too (the account stays as it is)."""
	user = make_user()
	wrong(client_for(), user.username, 5)
	assert db_get(User, user.id).failed_sign_ins == 5
	if action == "enable":
		client_for(admin).post(f"/admin/users/{user.id}/enable")
		assert db_get(User, user.id).is_active is True
	else:
		client_for(admin, xhr=True).post(f"/admin/users/{user.id}/reset_password")
	assert db_get(User, user.id).failed_sign_ins == 0


def test_the_factory_admin_is_locked_for_15_minutes_not_disabled(
		client_for, make_user, db_get, session_scope):
	"""The 5th wrong password locks the factory admin (audited
	auth.account_lock_timed) and says so; it stays active. While locked even
	the right password is refused with the minutes left."""
	factory = make_user(username="admin", role="admin")
	client = client_for()
	shown = wrong(client, "admin", 5)
	assert shown[:4] == [["Invalid credentials"]] * 4
	assert shown[4] == ["Too many failed sign-ins. Try again in 15 minutes."]
	row = db_get(User, factory.id)
	assert row.is_active is True
	assert row.locked_until is not None
	assert actions(session_scope, "auth.account_lock_timed") == [
		("admin", False, {"failed_passwords": 5, "minutes": 15})]
	resp = login(client, "admin")
	assert resp.headers["Location"] == "/"
	assert flashes(client) == ["Too many failed sign-ins. Try again in 15 minutes."]


def test_the_factory_admins_lock_runs_out(client_for, make_user, db_get, session_scope):
	"""A lock that has run out is lifted at the next sign-in: the right
	password signs in, the lock and count are gone. One minute left reads
	"1 minute"."""
	factory = make_user(username="admin", role="admin")
	with session_scope() as s:
		s.get(User, factory.id).locked_until = (
			dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30))
	client = client_for()
	login(client, "admin")
	assert flashes(client) == ["Too many failed sign-ins. Try again in 1 minute."]
	with session_scope() as s:
		s.get(User, factory.id).locked_until = (
			dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))
	assert login(client, "admin").headers["Location"] == "/dashboard"
	row = db_get(User, factory.id)
	assert row.locked_until is None and row.failed_sign_ins == 0


def test_ldap_accounts_are_not_counted(client_for, make_user, db_get, session_scope):
	"""A directory account's wrong passwords are the directory's business: no
	count, no lockout, after six."""
	with session_scope() as s:
		server = LDAPServer(name="corp", host="ldap.test", port=389, use_ssl=False,
		                    base_dn="dc=corp", bind_type="simple", cn_identifier="uid",
		                    is_active=True)
		s.add(server)
		s.flush()
		server_id = server.id
	user = make_user(auth_type="ldap", ldap_server_id=server_id)
	with patch("src.accounts.ldap.Directory.user_bind", return_value=False):
		wrong(client_for(), user.username, 6)
	row = db_get(User, user.id)
	assert row.failed_sign_ins == 0 and row.is_active is True


def test_lock_message_wording():
	"""The minutes in words: one minute, several minutes."""
	assert users.lock_message(1) == "Too many failed sign-ins. Try again in 1 minute."
	assert users.lock_message(15) == "Too many failed sign-ins. Try again in 15 minutes."
