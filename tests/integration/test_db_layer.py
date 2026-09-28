"""Database layer against real PostgreSQL: migrations, retention SQL,
the encryption canary query, and backend health."""
import datetime as dt
import os
import subprocess
import sys
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from src.db.db_install import (_RETENTION_JOBS, _schedule_retention,
                               JOB_RETENTION_DAYS,
                               CONFIG_SNAPSHOT_RETENTION_DAYS)
from tests.integration.conftest import ROOT

pytestmark = pytest.mark.postgres


# ── Migrations ───────────────────────────────────────────────────────────────

def test_models_match_migrated_schema(test_db_url):
	# `alembic check` fails if the ORM models and head migration diverge
	result = subprocess.run([sys.executable, "-m", "alembic", "check"],
	                        cwd=ROOT / "src" / "db", capture_output=True,
	                        text=True, env=dict(os.environ, DATABASE_URL=test_db_url))
	assert result.returncode == 0, result.stdout + result.stderr


def test_is_global_migration_backfills_and_round_trips(test_db_url):
	# Own throwaway DB: downgrade must not disturb the shared test DB
	name = f"rollout_mig_{uuid.uuid4().hex[:6]}"
	admin_url = test_db_url.rsplit("/", 1)[0] + "/postgres"
	url = test_db_url.rsplit("/", 1)[0] + "/" + name
	admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
	alembic = lambda *args: subprocess.run(
		[sys.executable, "-m", "alembic", *args], cwd=ROOT / "src" / "db",
		capture_output=True, text=True, env=dict(os.environ, DATABASE_URL=url))
	with admin.connect() as c:
		c.execute(text(f'CREATE DATABASE "{name}"'))
	engine = create_engine(url)
	try:
		assert alembic("upgrade", "5c2b80c49fc9").returncode == 0
		with engine.begin() as c:  # a device that predates is_global
			c.execute(text(
				"insert into users (id, username, role, is_active, is_approved,"
				" created_at, auth_type) values (:u,'seed','user',true,true,"
				"now(),'local')"), {"u": uuid.uuid4()})
			c.execute(text(
				"insert into inventory (id, ip, device_type, port, label, "
				"user_id) select :d, '10.0.0.1', 'cisco_ios', 22, 'old', id "
				"from users"), {"d": uuid.uuid4()})
		assert alembic("upgrade", "head").returncode == 0
		with engine.connect() as c:
			assert c.execute(text("select is_global from inventory")).scalar() \
			       is False
		assert alembic("downgrade", "-1").returncode == 0
		assert alembic("upgrade", "head").returncode == 0
	finally:
		engine.dispose()
		with admin.connect() as c:
			c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
		admin.dispose()


# ── Retention SQL (the statements pg_cron runs) ──────────────────────────────

@pytest.fixture
def retention_db(app):
	engine = app.backend.postgres.engine
	user = uuid.uuid4()
	now = dt.datetime.now()
	with engine.begin() as c:
		c.execute(text("insert into users (id, username, role, is_active, "
		               "is_approved, created_at, auth_type) values (:u, 'r', "
		               "'user', true, true, now(), 'local')"), {"u": user})
	jobs = {}

	def result(key, completed_days, config="cfg"):
		jobs[key] = uuid.uuid4()
		with engine.begin() as c:
			c.execute(text(
				"insert into device_results (id, job_id, started_at, "
				"completed_at, device_ip, device_type, commands_sent, "
				"commands_verified, fetched_config, status, user_id) values "
				"(gen_random_uuid(), :j, :t, :t, '10.0.0.1', 'cisco_ios', 2, "
				"1, :cfg, 'partial', :u)"),
				{"j": jobs[key], "t": now - dt.timedelta(days=completed_days),
				 "cfg": config, "u": user})

	def metadata(key, created_days):
		jobs.setdefault(key, uuid.uuid4())
		with engine.begin() as c:
			c.execute(text(
				"insert into job_metadata (id, job_id, commands, created_at, "
				"user_id) values (gen_random_uuid(), :j, '[\"x\"]', :t, :u)"),
				{"j": jobs[key], "t": now - dt.timedelta(days=created_days),
				 "u": user})

	def run_policy():
		with engine.begin() as c:
			for name in ("device_result_retention", "job_metadata_retention",
			             "device_result_config_retention"):
				c.execute(text(_RETENTION_JOBS[name]))
		with engine.connect() as c:
			results = dict(c.execute(text(
				"select job_id, fetched_config from device_results")).all())
			metas = {r[0] for r in c.execute(text(
				"select job_id from job_metadata")).all()}
		return results, metas

	return SimpleJobs(jobs, result, metadata, run_policy)


class SimpleJobs:
	def __init__(self, jobs, result, metadata, run_policy):
		self.jobs, self.result, self.metadata = jobs, result, metadata
		self.run_policy = run_policy


OLD, EDGE = JOB_RETENTION_DAYS + 5, JOB_RETENTION_DAYS - 1
SNAP_OLD = CONFIG_SNAPSHOT_RETENTION_DAYS + 3


def test_results_older_than_window_are_deleted(retention_db):
	retention_db.result("old", OLD)
	retention_db.result("edge", EDGE)
	results, _ = retention_db.run_policy()
	assert retention_db.jobs["old"] not in results
	assert retention_db.jobs["edge"] in results


def test_metadata_expires_together_with_its_results(retention_db):
	retention_db.result("gone", OLD)
	retention_db.metadata("gone", OLD)
	# submitted just past the window, but its results completed inside it
	retention_db.result("alive", EDGE)
	retention_db.metadata("alive", JOB_RETENTION_DAYS + 1)
	retention_db.metadata("crashed", OLD)  # job that never produced results
	_, metas = retention_db.run_policy()
	assert retention_db.jobs["gone"] not in metas
	assert retention_db.jobs["alive"] in metas
	assert retention_db.jobs["crashed"] not in metas


def test_config_snapshot_cleared_early_row_kept(retention_db):
	retention_db.result("stale", SNAP_OLD, config="big-config")
	retention_db.result("fresh", 1, config="big-config")
	results, _ = retention_db.run_policy()
	assert retention_db.jobs["stale"] in results
	assert results[retention_db.jobs["stale"]] is None
	assert results[retention_db.jobs["fresh"]] == "big-config"


@pytest.mark.pg_cron
def test_retention_jobs_schedule_in_pg_cron():
	"""pg_cron only schedules inside its own database (the live one), so this
	is opt-in: set TEST_PG_CRON_URL. Runs in a transaction that is ROLLED
	BACK — the live cron.job table is left untouched."""
	url = os.environ.get("TEST_PG_CRON_URL")
	if not url:
		pytest.skip("pg_cron test is opt-in: set TEST_PG_CRON_URL")
	engine = create_engine(url)
	conn = engine.connect()
	trans = conn.begin()
	try:
		for name, stmt in _RETENTION_JOBS.items():
			_schedule_retention(conn, name, stmt)
		scheduled = dict(conn.execute(text(
			"select jobname, command from cron.job")).all())
		for name, stmt in _RETENTION_JOBS.items():
			assert scheduled.get(name) == stmt
	finally:
		trans.rollback()
		conn.close()
		engine.dispose()


# ── Encryption canary + health ───────────────────────────────────────────────

def test_encrypted_sample_finds_fernet_values_only(app, make_user):
	from src.db.tables import SecurityProfile
	backend = app.backend
	assert backend.encrypted_sample() is None  # fresh DB
	owner = make_user()
	token = Fernet(Fernet.generate_key()).encrypt(b"pw").decode()
	with backend.postgres.get_session() as s:
		s.add(SecurityProfile(label="legacy", username="u",
		                      password_secret="plaintext-legacy",
		                      user_id=owner.id))
	assert backend.encrypted_sample() is None  # legacy plaintext ignored
	with backend.postgres.get_session() as s:
		s.add(SecurityProfile(label="real", username="u",
		                      password_secret=token, user_id=owner.id))
	assert backend.encrypted_sample() == token


def test_startup_canary_refuses_mismatched_key_against_real_db(app, make_user):
	import src.encryption as enc
	from src.db.tables import SecurityProfile
	from src.webapp.setup import init_app_encryption
	owner = make_user()
	foreign = Fernet(Fernet.generate_key()).encrypt(b"pw").decode()
	with app.backend.postgres.get_session() as s:
		s.add(SecurityProfile(label="x", username="u", password_secret=foreign,
		                      user_id=owner.id))
	with pytest.raises(enc.EncryptionStartupError, match="does not match"):
		init_app_encryption(app.backend)


@pytest.mark.redis
def test_health_reports_each_service(app):
	assert app.backend.health() == {"POSTGRES": True, "REDIS": True}
