"""Database moves for real, on the dev stack's Postgres: a target prepared
with an administrator login (another database, a schema of its own, a login
that isn't a superuser, another application's table beside it), the move
there, and back to the test database (the "bundled" one here). The app is
always put back on the test database, whatever happens."""
import time
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text

from src.db import move
from src.db.backend import BUNDLED_DATABASE_KEY
from src.db.postgres_db import PostgresConfig
from src.encryption import decrypt
from src.webapp import db_move
from src.webapp.db_move import DatabaseMove

from tests.integration.conftest import PG_ADMIN_URL

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def _admin_engine():
	return create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")


@pytest.fixture
def target():
	"""Prepared the way an admin login does it (src/db/move.prepare_with_admin),
	plus another application's table in the same schema."""
	admin = make_url(PG_ADMIN_URL)
	suffix = uuid.uuid4().hex[:8]
	plan = move.Plan(database=f"rollout_move_{suffix}", schema="nr",
	                 login=f"nr_move_{suffix}", grafana_password=None)
	done = move.prepare_with_admin(admin.host, str(admin.port or 5432), admin.username,
	                               admin.password, plan)
	assert f"login {plan.login} created" in done and f"database {plan.database} created" in done
	config = PostgresConfig(host=admin.host, port=str(admin.port or 5432),
	                        database=plan.database, user=plan.login,
	                        password=plan.password, schema="nr")
	other = create_engine(PostgresConfig(host=config.host, port=config.port,
	                                     database=plan.database, user=admin.username,
	                                     password=admin.password).get_url())
	with other.begin() as c:
		c.execute(text("CREATE TABLE nr.other_app_stuff (id int)"))
		c.execute(text("INSERT INTO nr.other_app_stuff VALUES (7)"))
		c.execute(text(f'ALTER TABLE nr.other_app_stuff OWNER TO "{plan.login}"'))
	other.dispose()
	yield config
	with _admin_engine().connect() as c:
		c.execute(text(f'DROP DATABASE IF EXISTS "{plan.database}" WITH (FORCE)'))
		c.execute(text(f'DROP ROLE IF EXISTS "{plan.login}"'))


@pytest.fixture
def mover(app, monkeypatch):
	home = app.backend.postgres.config
	monkeypatch.setattr(app.shutdown, "begin", lambda *a, **k: True)
	runtime_env = app.backend._CONFIG_ENV
	saved = runtime_env.read_text() if runtime_env.exists() else None
	yield DatabaseMove(app, poll_seconds=0.05)
	app.maintenance.end()
	if app.backend.postgres.config != home:     # never leave the app elsewhere
		app.backend.postgres.reload_db(home, install_flag=False)
	if saved is None:
		runtime_env.unlink(missing_ok=True)
	else:
		runtime_env.write_text(saved)


def _wait(mover, timeout=60):
	end = time.time() + timeout
	while mover.running and time.time() < end:
		time.sleep(0.05)
	assert not mover.running, mover.status()
	return mover.status()


def _rows(engine, sql):
	with engine.connect() as c:
		return c.execute(text(sql)).all()


def test_checking_a_prepared_target(target):
	report = move.check_target(target)
	assert report.ok, report.problems
	assert report.contents == move.EMPTY and report.schema == "nr"
	assert report.others == ["other_app_stuff"]
	assert report.pg_cron is False        # pg_cron lives in the bundled netrollout db only
	assert any("left alone" in n for n in report.notes)
	assert any("nightly clean-up itself" in n for n in report.notes)


def test_a_table_under_one_of_netrollouts_names_refuses_it(target):
	owner = create_engine(target.get_url())
	with owner.begin() as c:
		c.execute(text("CREATE TABLE nr.users (id int)"))
	owner.dispose()
	report = move.check_target(target)
	assert not report.ok and report.contents == move.CLASH
	assert "users" in report.problems[0]


def test_a_login_without_rights_on_the_schema_is_refused(target):
	with _admin_engine().connect() as c:
		c.execute(text(f'DROP ROLE IF EXISTS nr_move_nobody'))
		c.execute(text("CREATE ROLE nr_move_nobody LOGIN PASSWORD 'x-pass-1'"))
	try:
		report = move.check_target(PostgresConfig(
			host=target.host, port=target.port, database=target.database,
			user="nr_move_nobody", password="x-pass-1", schema="nr"))
		assert not report.ok and "can't create tables" in " ".join(report.problems)
	finally:
		with _admin_engine().connect() as c:
			c.execute(text("DROP ROLE nr_move_nobody"))


def test_an_unreachable_server_is_refused():
	report = move.check_target(PostgresConfig(host="127.0.0.1", port="1", database="x",
	                                          user="x", password="x"))
	assert not report.ok and report.problems[0].startswith("Couldn't connect")


def test_move_there_and_back(app, target, mover, make_user, make_profile):
	admin = make_user(role="admin")
	make_profile(admin, password="device-secret")
	home = app.backend.postgres.config
	source_users = _rows(app.backend.postgres.engine, "select id, username from users order by id")

	mover.start(target, admin.id, admin.username)
	status = _wait(mover)
	assert status["state"] == db_move.DONE, status
	assert app.backend.postgres.config == target
	assert app.maintenance.state == "idle" and app.orchestrator.refusal() is None
	engine = app.backend.postgres.engine
	assert _rows(engine, "select id, username from users order by id") == source_users
	(secret,), = _rows(engine, "select password_secret from security_profiles")
	assert decrypt(secret) == "device-secret"
	assert _rows(engine, "select id from nr.other_app_stuff") == [(7,)]   # left alone
	moved = _rows(engine, "select detail from audit_log where action = 'database.moved'")
	assert moved[0][0]["by"] == admin.username
	assert (app.backend.bundled_postgres().get_url() ==
	        make_url(home.get_url()).render_as_string(hide_password=False))
	assert BUNDLED_DATABASE_KEY in app.backend._CONFIG_ENV.read_text()
	assert status["backup"].endswith("-before-move.zip")

	mover.start(app.backend.bundled_postgres(), admin.id, admin.username, back=True)
	status = _wait(mover)
	assert status["state"] == db_move.DONE, status
	assert db_move.same_database(app.backend.postgres.config, home)
	assert _rows(app.backend.postgres.engine,
	             "select id, username from users order by id") == source_users
	assert len(_rows(app.backend.postgres.engine,
	                 "select 1 from audit_log where action = 'database.moved'")) == 2


def test_the_same_database_or_a_refused_target_never_starts(app, target, mover):
	with pytest.raises(move.MoveError, match="uses now"):
		mover.start(app.backend.postgres.config, None, "admin")
	owner = create_engine(target.get_url())
	with owner.begin() as c:
		c.execute(text("CREATE TABLE nr.users (id int)"))
	owner.dispose()
	with pytest.raises(move.MoveError, match="another application"):
		mover.start(target, None, "admin")
	assert app.maintenance.state == "idle"


def test_cancelled_while_waiting_for_rollouts(app, target, mover, monkeypatch):
	monkeypatch.setattr(app.orchestrator, "idle", lambda: False)    # a rollout runs
	mover.start(target, None, "admin")
	assert app.orchestrator.refusal() is not None
	time.sleep(0.2)
	assert mover.status()["state"] == db_move.WAITING
	assert mover.cancel()
	status = _wait(mover)
	assert status["state"] == db_move.CANCELLED
	assert app.maintenance.state == "idle" and app.orchestrator.refusal() is None
	assert _rows(app.backend.postgres.engine,
	             "select 1 from audit_log where action = 'database.move_cancelled'")


def test_rollouts_still_running_after_the_wait_give_the_move_up(app, target, monkeypatch, mover):
	monkeypatch.setattr(app.orchestrator, "idle", lambda: False)
	short = DatabaseMove(app, wait_seconds=0.3, poll_seconds=0.05)
	short.start(target, None, "admin")
	status = _wait(short)
	assert status["state"] == db_move.FAILED and "Cancel the stuck rollouts" in status["message"]
	assert app.maintenance.state == "idle"


def test_a_failed_copy_leaves_everything_as_it_was(app, target, mover, monkeypatch):
	home = app.backend.postgres.config

	def broken(*a, **k):
		raise move.MoveError("simulated: the copy failed - nothing was changed.")
	monkeypatch.setattr(db_move.move, "copy", broken)
	mover.start(target, None, "admin")
	status = _wait(mover)
	assert status["state"] == db_move.FAILED and "simulated" in status["message"]
	assert app.backend.postgres.config == home and app.maintenance.state == "idle"
	assert _rows(app.backend.postgres.engine,
	             "select 1 from audit_log where action = 'database.move_failed'")
