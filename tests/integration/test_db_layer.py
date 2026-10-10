"""Database layer against real PostgreSQL: migrations, retention SQL,
the encryption canary query, and backend health."""
import datetime as dt
import os
import subprocess
import sys
import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

import src.encryption as enc
from src.db.connections import PostgresConfig, PostgresConnection
from src.db.install import _grant_grafana_read, GRAFANA_TABLES, install
from src.db.retention import RETENTION_STATEMENTS
from src.db.settings import SETTINGS
from src.db.tables import SecurityProfile
from src.webapp.build import init_app_encryption
from tests.integration.conftest import ROOT

pytestmark = pytest.mark.postgres


# ── Migrations ───────────────────────────────────────────────────────────────

def test_models_match_migrated_schema(test_db_url):
	"""`alembic check` passes on the migrated test database: the ORM models and the
	head migration don't diverge."""
	result = subprocess.run([sys.executable, "-m", "alembic", "check"],
	                        cwd=ROOT / "src" / "db", capture_output=True,
	                        text=True, env=dict(os.environ, DATABASE_URL=test_db_url))
	assert result.returncode == 0, result.stdout + result.stderr


def test_device_results_are_indexed_for_results_and_a_job(test_db_url):
	"""The migrated device_results has an index on (user_id, job_id) - Results
	pages a user's jobs - and one on job_id - a job's page reads its devices."""
	indexes = {ix["name"]: ix["column_names"]
	           for ix in inspect(create_engine(test_db_url)).get_indexes("device_results")}
	assert indexes.get("ix_device_results_user_id_job_id") == ["user_id", "job_id"]
	assert indexes.get("ix_device_results_job_id") == ["job_id"]


def test_baseline_round_trips_and_sets_server_defaults(test_db_url):
	"""At head, rows inserted without is_global / device_port get the server
	defaults (false, 22); downgrading to base leaves no app table and upgrading
	again rebuilds them."""
	# Own throwaway DB: downgrade must not disturb the shared test DB
	name = f"rollout_mig_{uuid.uuid4().hex[:6]}"
	admin_url = test_db_url.rsplit("/", 1)[0] + "/postgres"
	url = test_db_url.rsplit("/", 1)[0] + "/" + name
	admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
	alembic = lambda *args: subprocess.run(
		[sys.executable, "-m", "alembic", *args], cwd=ROOT / "src" / "db",
		capture_output=True, text=True, env=dict(os.environ, DATABASE_URL=url))
	app_tables = text("select count(*) from pg_tables where schemaname = "
	                  "'public' and tablename <> 'alembic_version'")
	with admin.connect() as c:
		c.execute(text(f'CREATE DATABASE "{name}"'))
	engine = create_engine(url)
	try:
		assert alembic("upgrade", "head").returncode == 0
		# Rows written without the columns (raw SQL, old clients) get the
		# server defaults the development history had
		with engine.begin() as c:
			c.execute(text(
				"insert into users (id, username, role, is_active, is_approved,"
				" created_at, auth_type) values (:u,'seed','user',true,true,"
				"now(),'local')"), {"u": uuid.uuid4()})
			c.execute(text(
				"insert into inventory (id, ip, device_type, port, label, "
				"user_id) select :d, '10.0.0.1', 'cisco_ios', 22, 'old', id "
				"from users"), {"d": uuid.uuid4()})
			c.execute(text(
				"insert into device_results (id, job_id, started_at, "
				"completed_at, device_ip, device_type, commands_sent, status, "
				"user_id) select :r, :j, now(), now(), '10.0.0.1', "
				"'cisco_ios', 1, 'success', id from users"),
				{"r": uuid.uuid4(), "j": uuid.uuid4()})
		with engine.connect() as c:
			assert c.execute(text("select is_global from inventory")).scalar() \
			       is False
			assert c.execute(text("select device_port from device_results")) \
			       .scalar() == 22
		# The baseline's downgrade empties the database; upgrading rebuilds it
		assert alembic("downgrade", "base").returncode == 0
		with engine.connect() as c:
			assert c.execute(app_tables).scalar() == 0
		assert alembic("upgrade", "head").returncode == 0
		with engine.connect() as c:
			assert c.execute(app_tables).scalar() > 0
	finally:
		engine.dispose()
		with admin.connect() as c:
			c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
		admin.dispose()


@pytest.fixture
def scratch_db(test_db_url):
	"""A throwaway database (no DATABASE_URL pointing at it)."""
	name = f"rollout_inst_{uuid.uuid4().hex[:6]}"
	base = make_url(test_db_url)
	admin = create_engine(base.set(database="postgres"),
	                      isolation_level="AUTOCOMMIT")
	with admin.connect() as c:
		c.execute(text(f'CREATE DATABASE "{name}"'))
	yield base.set(database=name)
	with admin.connect() as c:
		c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
	admin.dispose()


def _pg_config(url, schema=None):
	return PostgresConfig(host=url.host, port=str(url.port),
	                      database=url.database, user=url.username,
	                      password=url.password, schema=schema)


def _head_revision():
	"""The head revision of the project's Alembic scripts."""
	return ScriptDirectory.from_config(
		Config(str(ROOT / "src" / "db" / "alembic.ini"))).get_current_head()


def test_install_migrates_the_connected_db_not_database_url(
		scratch_db, monkeypatch):
	"""install() migrates the database it is connected to (no DATABASE_URL) to head
	and seeds one admin who must change the password."""
	monkeypatch.delenv("DATABASE_URL", raising=False)  # used to be required
	conn = PostgresConnection(_pg_config(scratch_db))
	try:
		install(conn)
		with conn.engine.connect() as c:
			assert c.execute(text("select version_num from alembic_version")) \
				       .scalar() == _head_revision()
			assert c.execute(text("select count(*) from users where "
			                      "username='admin'")).scalar() == 1
			# the seeded admin must pick its own password first (stage 4)
			assert c.execute(text("select must_change_password from users "
			                      "where username='admin'")).scalar() is True
	finally:
		conn.disconnect()


def test_install_honours_pg_schema(scratch_db):
	"""With a schema configured, install() creates the tables (alembic_version
	included) in that schema and none in public."""
	admin = create_engine(scratch_db)
	with admin.begin() as c:
		c.execute(text("CREATE SCHEMA nr"))
	admin.dispose()
	conn = PostgresConnection(_pg_config(scratch_db, schema="nr"))
	try:
		install(conn)
		with conn.engine.connect() as c:
			tables = {r[0] for r in c.execute(text(
				"select table_name from information_schema.tables "
				"where table_schema = 'nr'"))}
			public = c.execute(text(
				"select count(*) from information_schema.tables "
				"where table_schema = 'public'")).scalar()
	finally:
		conn.disconnect()
	assert {"users", "inventory", "alembic_version"} <= tables
	assert public == 0  # nothing leaked into public


def test_cli_migrates_from_pg_vars_without_database_url(scratch_db):
	"""The alembic CLI, given only PG_* variables (no DATABASE_URL), migrates that
	database to head."""
	# Every PG_* var is set explicitly: config/runtime.env is loaded without override,
	# so anything left unset would be filled from the developer's live config
	env = {k: v for k, v in os.environ.items()
	       if k != "DATABASE_URL" and not k.startswith("PG_")}
	env.update(PG_HOST=scratch_db.host, PG_PORT=str(scratch_db.port),
	           PG_NAME=scratch_db.database, PG_USER=scratch_db.username,
	           PG_PASSWORD=scratch_db.password)
	result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
	                        cwd=ROOT / "src" / "db", capture_output=True,
	                        text=True, env=env)
	assert result.returncode == 0, result.stderr
	engine = create_engine(scratch_db)
	with engine.connect() as c:
		assert c.execute(text("select version_num from alembic_version")) \
			       .scalar() == _head_revision()
	engine.dispose()


# ── Retention SQL (the statements the nightly clean-up runs) ──────────────────────────────

@pytest.fixture
def retention_db(app):
	"""Helpers on the test database for one user: result(key, days) and
	metadata(key, days) add a job's rows that old, run_policy() runs the three
	job retention statements and returns {job_id: fetched_config} and the job ids
	with metadata left."""
	engine = app.backend.postgres.engine
	user = uuid.uuid4()
	now = dt.datetime.now()
	with engine.begin() as c:
		c.execute(text("insert into users (id, username, role, is_active, "
		               "is_approved, created_at, auth_type) values (:u, 'r', "
		               "'operator', true, true, now(), 'local')"), {"u": user})
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
				c.execute(text(RETENTION_STATEMENTS[name]))
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


JOB_RETENTION_DAYS = SETTINGS["job_retention_days"].default
CONFIG_SNAPSHOT_RETENTION_DAYS = SETTINGS["config_snapshot_retention_days"].default
OLD, EDGE = JOB_RETENTION_DAYS + 5, JOB_RETENTION_DAYS - 1
SNAP_OLD = CONFIG_SNAPSHOT_RETENTION_DAYS + 3


def test_results_older_than_window_are_deleted(retention_db):
	"""Results completed past the job retention window are deleted; one a day
	inside it stays."""
	retention_db.result("old", OLD)
	retention_db.result("edge", EDGE)
	results, _ = retention_db.run_policy()
	assert retention_db.jobs["old"] not in results
	assert retention_db.jobs["edge"] in results


def test_metadata_expires_together_with_its_results(retention_db):
	"""Job metadata goes with its expired results, stays while its results are
	inside the window (even if submitted past it), and an old job without results
	is deleted."""
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
	"""Past the config snapshot window the fetched config is cleared but the result
	row stays; a fresh one keeps its config."""
	retention_db.result("stale", SNAP_OLD, config="big-config")
	retention_db.result("fresh", 1, config="big-config")
	results, _ = retention_db.run_policy()
	assert retention_db.jobs["stale"] in results
	assert results[retention_db.jobs["stale"]] is None
	assert results[retention_db.jobs["fresh"]] == "big-config"


def test_retention_follows_the_system_setting(app, retention_db):
	"""The statements read the System Settings when they run (no restart needed):
	shorter windows delete a 15-day result and clear a 5-day snapshot; back to the
	defaults, a 15-day result survives again."""
	app.backend.settings.update({"job_retention_days": 10,
	                             "config_snapshot_retention_days": 2}, None)
	retention_db.result("fifteen", 15)                 # kept by the default (30)
	retention_db.result("five", 5, config="big-config")
	results, _ = retention_db.run_policy()
	assert retention_db.jobs["fifteen"] not in results
	assert results[retention_db.jobs["five"]] is None  # snapshot > 2 days: cleared
	# back to the default: a 15-day-old record survives again
	app.backend.settings.reset("config_snapshot_retention_days", None)
	app.backend.settings.reset("job_retention_days", None)
	retention_db.result("fifteen-again", 15)
	results, _ = retention_db.run_policy()
	assert retention_db.jobs["fifteen-again"] in results


def test_audit_retention_follows_the_setting(app):
	"""Audit retention keeps a 10-day row and deletes a 100-day one by default (90
	days); with the setting at 7 days the 10-day row goes too."""
	engine = app.backend.postgres.engine
	with engine.begin() as c:
		for days in (10, 100):
			c.execute(text(
				"insert into audit_log (id, timestamp, actor_username, action, "
				"success) values (gen_random_uuid(), now() - make_interval("
				"days => :d), 'x', :a, true)"), {"d": days, "a": f"age.{days}"})

	def remaining():
		with engine.begin() as c:
			c.execute(text(RETENTION_STATEMENTS["audit_log_retention"]))
			return {r[0] for r in c.execute(text("select action from audit_log"))}
	assert remaining() == {"age.10"}                   # default 90 days
	app.backend.settings.update({"audit_retention_days": 7}, None)
	assert remaining() == set()


def test_grafana_reads_exactly_the_dashboard_tables(app):
	"""The Grafana grant (run twice: idempotent) lets the role read every dashboard
	table and none of users, security_profiles, inventory, ldap_servers,
	system_settings - an earlier wider grant is taken back."""
	# Roles are cluster-wide: a throwaway role, dropped afterwards
	engine = app.backend.postgres.engine
	role = f"nr_test_reader_{uuid.uuid4().hex[:8]}"
	with engine.begin() as c:
		c.execute(text(f'CREATE ROLE "{role}"'))
	try:
		with engine.begin() as c:
			c.execute(text(f'GRANT SELECT ON users, inventory TO "{role}"'))
			assert _grant_grafana_read(c, role)
			assert _grant_grafana_read(c, role)       # every start: idempotent

			def can_read(table):
				return c.execute(text("SELECT has_table_privilege(:r, :t, "
				                      "'SELECT')"), {"r": role, "t": table}).scalar()
			assert all(can_read(t) for t in GRAFANA_TABLES)
			# credentials, password hashes, LDAP, settings: never — also a
			# wider grant from before is taken back
			for table in ("users", "security_profiles", "inventory",
			              "ldap_servers", "system_settings"):
				assert not can_read(table), table
	finally:
		with engine.begin() as c:
			c.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"'))
			c.execute(text(f'DROP ROLE "{role}"'))


def test_grafana_grant_is_skipped_without_the_role(app):
	"""Without the role (dev databases and external Postgres servers have no
	grafana_reader) the grant is skipped and returns False."""
	with app.backend.postgres.engine.begin() as c:
		assert _grant_grafana_read(c, "nr_no_such_role") is False


# ── Encryption canary + health ───────────────────────────────────────────────

def test_encrypted_sample_finds_fernet_values_only(app, make_user):
	"""encrypted_sample() is None on a fresh database and with only a plaintext
	legacy secret, and returns the Fernet token once one is stored."""
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
	"""A stored secret encrypted with another key makes the encryption start-up
	refuse ("does not match")."""
	owner = make_user()
	foreign = Fernet(Fernet.generate_key()).encrypt(b"pw").decode()
	with app.backend.postgres.get_session() as s:
		s.add(SecurityProfile(label="x", username="u", password_secret=foreign,
		                      user_id=owner.id))
	with pytest.raises(enc.EncryptionStartupError, match="does not match"):
		init_app_encryption(app.backend)


@pytest.mark.redis
def test_health_reports_each_service(app):
	"""health() reports Postgres and Redis both up."""
	assert app.backend.health() == {"POSTGRES": True, "REDIS": True}
