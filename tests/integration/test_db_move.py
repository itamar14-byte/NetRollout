"""Database moves for real, on the dev stack's Postgres: a target prepared
with an administrator login (another database, a schema of its own, a login
that isn't a superuser, another application's table beside it), the move
there, and back to the test database (the "bundled" one here). The app is
always put back on the test database, whatever happens."""
import time
import uuid

import pytest
from alembic import command as alembic_command
from sqlalchemy import create_engine, make_url, text

from src.backup import archive
from src.db import move
from src.db.connections import BUNDLED_DATABASE_KEY, PostgresConfig
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
	"""A DatabaseMove polling every 50 ms (the restart stubbed out); afterwards the
	maintenance ended, the app back on the test database and runtime.env restored."""
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
	"""Wait until the move has ended (asserted) and return its status."""
	end = time.time() + timeout
	while mover.running and time.time() < end:
		time.sleep(0.05)
	assert not mover.running, mover.status()
	return mover.status()


def _rows(engine, sql):
	with engine.connect() as c:
		return c.execute(text(sql)).all()


def test_checking_a_prepared_target(target):
	"""A prepared target passes the check: empty, schema nr, the other app's table
	listed and noted as left alone."""
	report = move.check_target(target)
	assert report.ok, report.problems
	assert report.contents == move.EMPTY and report.schema == "nr"
	assert report.others == ["other_app_stuff"]
	assert any("left alone" in n for n in report.notes)


def test_a_table_under_one_of_netrollouts_names_refuses_it(target):
	"""Another table named users in the target schema is a clash: refused, naming it."""
	owner = create_engine(target.get_url())
	with owner.begin() as c:
		c.execute(text("CREATE TABLE nr.users (id int)"))
	owner.dispose()
	report = move.check_target(target)
	assert not report.ok and report.contents == move.CLASH
	assert "users" in report.problems[0]


def test_a_login_without_rights_on_the_schema_is_refused(target):
	"""A login with no rights on the target schema is refused: it can't create tables."""
	with _admin_engine().connect() as c:
		c.execute(text('DROP ROLE IF EXISTS nr_move_nobody'))
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
	"""A server nobody listens on is refused, "Couldn't connect" first."""
	report = move.check_target(PostgresConfig(host="127.0.0.1", port="1", database="x",
	                                          user="x", password="x"))
	assert not report.ok and report.problems[0].startswith("Couldn't connect")


def test_move_there_and_back(app, target, mover, make_user, make_profile):
	"""A move ends done on the target with maintenance over: same users, the
	credential decrypts, the other app's table untouched, database.moved audited,
	the bundled address remembered in runtime.env, a before-move backup made.
	Moving back returns to the bundled database with the same users and a
	second database.moved row."""
	admin = make_user(role="admin")
	make_profile(admin, password="device-secret")
	home = app.backend.postgres.config
	source_users = _rows(app.backend.postgres.engine, "select id, username from users order by id")

	mover.start(target, admin.id, admin.username)
	status = _wait(mover)
	assert status["state"] == db_move.MoveState.DONE, status
	assert app.backend.postgres.config == target
	assert app.maintenance.state == "idle" and app.orchestrator.refusal() is None
	# on the same host as the bundled one, yet not it: Move back is offered
	assert app.backend.connection_modes()["POSTGRES"] == "external"
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

	mover.start(app.backend.bundled_postgres(), admin.id, admin.username, back=True,
	            replace=True)
	status = _wait(mover)
	assert status["state"] == db_move.MoveState.DONE, status
	assert db_move.same_database(app.backend.postgres.config, home)
	assert app.backend.connection_modes()["POSTGRES"] == "bundled"
	assert _rows(app.backend.postgres.engine,
	             "select id, username from users order by id") == source_users
	assert len(_rows(app.backend.postgres.engine,
	                 "select 1 from audit_log where action = 'database.moved'")) == 2


def test_the_same_database_or_a_refused_target_never_starts(app, target, mover):
	"""A move to the database in use, or to a target with a clashing table, is
	refused at start and maintenance never begins."""
	with pytest.raises(move.MoveError, match="uses now"):
		mover.start(app.backend.postgres.config, None, "admin")
	owner = create_engine(target.get_url())
	with owner.begin() as c:
		c.execute(text("CREATE TABLE nr.users (id int)"))
	owner.dispose()
	with pytest.raises(move.MoveError, match="another application"):
		mover.start(target, None, "admin")
	assert app.maintenance.state == "idle"


def holding_netrollout(target):
	"""The target with a NetRollout database in it (migrated to head)."""
	engine = move.engine_for(target)
	with engine.begin() as conn:
		alembic_command.upgrade(archive._alembic_config(conn), "head")
	engine.dispose()
	assert move.check_target(target).contents == move.NETROLLOUT
	return target


def test_a_netrollout_database_there_is_replaced_only_when_asked(app, target, mover):
	"""A target holding a NetRollout database is refused unless replace is asked
	("tick 'replace'"), maintenance never beginning; asked, the move replaces it
	and ends done."""
	holding_netrollout(target)
	with pytest.raises(move.MoveError, match="tick 'replace' to overwrite it"):
		mover.start(target, None, "admin")
	assert app.maintenance.state == "idle"
	mover.start(target, None, "admin", replace=True)
	assert _wait(mover)["state"] == db_move.MoveState.DONE
	assert app.backend.postgres.config == target


def test_cancelled_while_waiting_for_rollouts(app, target, mover, monkeypatch):
	"""A move waiting for a running rollout refuses new rollouts; cancelled, it ends
	cancelled, maintenance over, rollouts accepted again, database.move_cancelled
	audited."""
	monkeypatch.setattr(app.orchestrator, "idle", lambda: False)    # a rollout runs
	mover.start(target, None, "admin")
	assert app.orchestrator.refusal() is not None
	time.sleep(0.2)
	assert mover.status()["state"] == db_move.MoveState.WAITING
	assert mover.cancel()
	status = _wait(mover)
	assert status["state"] == db_move.MoveState.CANCELLED
	assert app.maintenance.state == "idle" and app.orchestrator.refusal() is None
	assert _rows(app.backend.postgres.engine,
	             "select 1 from audit_log where action = 'database.move_cancelled'")


def test_rollouts_still_running_after_the_wait_give_the_move_up(app, target, monkeypatch, mover):
	"""Rollouts still running when the wait runs out fail the move ("Cancel the
	stuck rollouts") and maintenance ends."""
	monkeypatch.setattr(app.orchestrator, "idle", lambda: False)
	short = DatabaseMove(app, wait_seconds=0.3, poll_seconds=0.05)
	short.start(target, None, "admin")
	status = _wait(short)
	assert status["state"] == db_move.MoveState.FAILED and "Cancel the stuck rollouts" in status["message"]
	assert app.maintenance.state == "idle"


def test_a_switch_failing_after_the_reconnect_goes_back_to_the_database(
		app, target, mover, monkeypatch):
	"""runtime.env failing to be written (after the live reconnect) ends the move
	failed, saying NetRollout stays on its database - and it does: the app's
	connection is back on it, runtime.env unchanged, database.move_failed audited
	there."""
	home = app.backend.postgres.config
	runtime_env = app.backend._CONFIG_ENV
	before = runtime_env.read_text() if runtime_env.exists() else None

	def read_only(updates):
		raise PermissionError(13, "Permission denied", str(runtime_env))
	monkeypatch.setattr(app.backend, "_write_config", read_only)
	mover.start(target, None, "admin")
	status = _wait(mover)
	assert status["state"] == db_move.MoveState.FAILED, status
	assert "Permission denied" in status["message"]
	assert "stays on its database" in status["message"]
	assert app.backend.postgres.config == home and app.maintenance.state == "idle"
	assert (runtime_env.read_text() if runtime_env.exists() else None) == before
	assert _rows(app.backend.postgres.engine,
	             "select 1 from audit_log where action = 'database.move_failed'")


def test_a_failed_copy_leaves_everything_as_it_was(app, target, mover, monkeypatch):
	"""A copy that fails ends the move failed with its message, the app still on
	its database, maintenance over and database.move_failed audited."""
	home = app.backend.postgres.config

	def broken(*a, **k):
		raise move.MoveError("simulated: the copy failed - nothing was changed.")
	monkeypatch.setattr(db_move.move, "copy", broken)
	mover.start(target, None, "admin")
	status = _wait(mover)
	assert status["state"] == db_move.MoveState.FAILED and "simulated" in status["message"]
	assert app.backend.postgres.config == home and app.maintenance.state == "idle"
	assert _rows(app.backend.postgres.engine,
	             "select 1 from audit_log where action = 'database.move_failed'")



def test_the_copy_takes_the_backup_lock_where_its_backup_is(app, target, tmp_path,
                                                            monkeypatch):
	"""The copy's backup and its restore into the target take the backup lock in the same
	folder (the places given) - the restore took it in the app's own backups folder."""
	places = archive.Places(tmp_path / "backups", tmp_path / "certs", tmp_path / "logs")
	taken = []
	real = archive._lock

	def recording(folder):
		taken.append(folder)
		return real(folder)
	monkeypatch.setattr(archive, "_lock", recording)
	move.copy(app.backend.postgres.engine, target, detail={}, places=places)
	assert taken and set(taken) == {places.backups}
