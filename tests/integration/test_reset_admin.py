"""`netrollout reset-admin` (python -m src.accounts reset-admin): the
factory admin made usable again on the server - enabled, 2FA off, wrong
passwords and lock cleared, signed out everywhere, audited with the server
as the actor; with --new-password a temporary password that must be
replaced."""
import datetime as dt

import pytest
from werkzeug.security import check_password_hash

from src.accounts import __main__ as accounts_cli
from src.accounts.__main__ import AccountsExit, reset_admin
from src.db.tables import AuditLog, User
from src.encryption import encrypt

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def broken_factory(make_user, session_scope):
	"""The factory admin disabled, with 2FA, 4 wrong passwords and a lock."""
	user = make_user(username="admin", role="admin")
	with session_scope() as s:
		row = s.get(User, user.id)
		row.is_active = False
		row.otp_secret = encrypt("JBSWY3DPEHPK3PXP")
		row.failed_sign_ins = 4
		row.locked_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
	return user


def test_reset_admin_makes_the_factory_admin_usable(app, broken_factory, client_for, db_get,
                                                    session_scope, capsys):
	"""Enabled, 2FA off, count and lock cleared, its session ended, the
	password kept; one user.reset_admin row naming the server as the actor."""
	client_for(broken_factory)        # a session of the factory admin's in Redis
	assert reset_admin(app.backend.postgres, app.backend.redis, False) is AccountsExit.OK
	row = db_get(User, broken_factory.id)
	assert (row.is_active, row.otp_secret, row.failed_sign_ins, row.locked_until) == (
		True, None, 0, None)
	assert row.must_change_password is False
	with session_scope() as s:
		audits = [(a.actor_username, a.actor_id, a.object_id, a.detail)
		          for a in s.query(AuditLog).filter_by(action="user.reset_admin")]
	assert audits == [("netrollout reset-admin (on the server)", None, broken_factory.id,
	                   {"new_password": False, "sessions_ended": 1})]
	out = capsys.readouterr().out
	assert "The factory admin (admin) is enabled, its two-factor sign-in is off" in out
	assert "It was signed out everywhere (1 session)." in out
	assert "Temporary password" not in out


def test_reset_admin_with_a_new_password(app, broken_factory, db_get, capsys):
	"""--new-password: a temporary password printed once, stored hashed, to
	be replaced at the next sign-in."""
	assert reset_admin(app.backend.postgres, app.backend.redis, True) is AccountsExit.OK
	out = capsys.readouterr().out
	line = next(l for l in out.splitlines() if l.startswith("Temporary password (shown once): "))
	temporary = line.split(": ", 1)[1]
	row = db_get(User, broken_factory.id)
	assert check_password_hash(row.password_hash, temporary)
	assert row.must_change_password is True


def test_reset_admin_without_a_factory_admin_changes_nothing(app, make_user, session_scope,
                                                             capsys):
	"""No factory admin in the database: refused in words (exit 1), nothing
	audited."""
	make_user(role="admin")
	assert reset_admin(app.backend.postgres, app.backend.redis, False) is AccountsExit.FAILED
	assert "There is no factory admin account (admin)" in capsys.readouterr().err
	with session_scope() as s:
		assert s.query(AuditLog).filter_by(action="user.reset_admin").count() == 0


def test_the_command_line(monkeypatch):
	"""reset-admin [--new-password] reaches reset_admin with the flag."""
	seen = []
	monkeypatch.setattr(accounts_cli, "load_config", lambda path: None)
	monkeypatch.setattr(accounts_cli, "reset_admin",
	                    lambda postgres, redis_conn, new: seen.append(new) or AccountsExit.OK)
	assert accounts_cli.main(["reset-admin"]) is AccountsExit.OK
	assert accounts_cli.main(["reset-admin", "--new-password"]) is AccountsExit.OK
	assert seen == [False, True]
