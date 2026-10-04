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
                                 _grant_grafana_read, GRAFANA_TABLES)
from src.db.settings import SETTINGS
from tests.integration.conftest import ROOT

pytestmark = pytest.mark.postgres


# ── Migrations ───────────────────────────────────────────────────────────────

def test_models_match_migrated_schema(test_db_url):
	# `alembic check` fails if the ORM models and head migration diverge
	result = subprocess.run([sys.executable, "-m", "alembic", "check"],
	                        cwd=ROOT / "src" / "db", capture_output=True,
	                        text=True, env=dict(os.environ, DATABASE_URL=test_db_url))
	assert result.returncode == 0, result.stdout + result.stderr


def test_must_change_password_migration_flags_only_a_factory_admin(
		test_db_url):
	# An install upgraded from the v1.0.0 baseline: its admin still on
	# "admin" gets the flag, one that changed it (and anyone else) doesn't
	from werkzeug.security import generate_password_hash
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
		for passwords in ({"admin": "admin", "bob": "admin"},
		                  {"admin": "Changed-123"}):
			assert alembic("upgrade", "v1_0_0_baseline").returncode == 0
			with engine.begin() as c:
				c.execute(text("delete from users"))
				for username, password in passwords.items():
					c.execute(text(
						"insert into users (id, username, password_hash, role,"
						" is_active, is_approved, created_at, auth_type) values"
						" (:i, :u, :h, 'admin', true, true, now(), 'local')"),
						{"i": uuid.uuid4(), "u": username,
						 "h": generate_password_hash(password)})
			assert alembic("upgrade", "head").returncode == 0
			with engine.connect() as c:
				flags = dict(c.execute(text(
					"select username, must_change_password from users")).all())
			expected = {u: (u == "admin" and p == "admin")
			            for u, p in passwords.items()}
			assert flags == expected
			assert alembic("downgrade", "v1_0_0_baseline").returncode == 0
	finally:
		engine.dispose()
		with admin.connect() as c:
			c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
		admin.dispose()


def test_baseline_round_trips_and_sets_server_defaults(test_db_url):
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
	"""A throwaway database (no pg_cron, no DATABASE_URL pointing at it)."""
	from sqlalchemy.engine import make_url
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
	from src.db.postgres_db import PostgresConfig
	return PostgresConfig(host=url.host, port=str(url.port),
	                      database=url.database, user=url.username,
	                      password=url.password, schema=schema)


def _head_revision():
	from alembic.config import Config
	from alembic.script import ScriptDirectory
	return ScriptDirectory.from_config(
		Config(str(ROOT / "src" / "db" / "alembic.ini"))).get_current_head()


def test_install_migrates_the_connected_db_not_database_url(
		scratch_db, monkeypatch, capsys):
	from src.db.db_install import install
	from src.db.postgres_db import PostgresConnection
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
	# no pg_cron here: warned about, and it didn't block the schema
	assert "Retention jobs not scheduled" in capsys.readouterr().out


def test_install_honours_pg_schema(scratch_db):
	from src.db.db_install import install
	from src.db.postgres_db import PostgresConnection
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
	# Every PG_* var is set explicitly: config.env is loaded without override,
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


# ── Retention SQL (the statements pg_cron runs) ──────────────────────────────

@pytest.fixture
def retention_db(app):
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


JOB_RETENTION_DAYS = SETTINGS["job_retention_days"].default
CONFIG_SNAPSHOT_RETENTION_DAYS = SETTINGS["config_snapshot_retention_days"].default
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


def test_retention_follows_the_system_setting(app, retention_db):
	# the statements read the setting when they run: no restart needed
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
	engine = app.backend.postgres.engine
	with engine.begin() as c:
		for days in (10, 100):
			c.execute(text(
				"insert into audit_log (id, timestamp, actor_username, action, "
				"success) values (gen_random_uuid(), now() - make_interval("
				"days => :d), 'x', :a, true)"), {"d": days, "a": f"age.{days}"})

	def remaining():
		with engine.begin() as c:
			c.execute(text(_RETENTION_JOBS["audit_log_retention"]))
			return {r[0] for r in c.execute(text("select action from audit_log"))}
	assert remaining() == {"age.10"}                   # default 90 days
	app.backend.settings.update({"audit_retention_days": 7}, None)
	assert remaining() == set()


def test_grafana_reads_exactly_the_dashboard_tables(app):
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
	# dev databases and external Postgres servers have no grafana_reader
	with app.backend.postgres.engine.begin() as c:
		assert _grant_grafana_read(c, "nr_no_such_role") is False


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


def test_role_migration_renames_user_to_operator(test_db_url):
	# An install from before the rename: its operators (and LDAP group rules)
	# stored as "user" become "operator"; admins stay; downgrade reverses it
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

	def roles():
		with engine.connect() as c:
			return (dict(c.execute(text("select username, role from users")).all()),
			        dict(c.execute(text("select label, role from ldap_groups")).all()))
	try:
		assert alembic("upgrade", "device_results_action_needed").returncode == 0
		server = uuid.uuid4()
		with engine.begin() as c:
			for username, role in (("ops", "user"), ("boss", "admin")):
				c.execute(text(
					"insert into users (id, username, password_hash, role, is_active,"
					" is_approved, created_at, auth_type, must_change_password)"
					" values (:i, :u, 'x', :r, true, true, now(), 'local', false)"),
					{"i": uuid.uuid4(), "u": username, "r": role})
			c.execute(text(
				"insert into ldap_servers (id, name, host, port, base_dn,"
				" cn_identifier, bind_type, use_ssl, is_active) values"
				" (:i, 'dc', 'dc.local', 389, 'dc=x', 'uid', 'anonymous', false, true)"),
				{"i": server})
			for label, role in (("netops", "user"), ("leads", "admin")):
				c.execute(text(
					"insert into ldap_groups (id, group_dn, label, role, is_active,"
					" ldap_server_id) values (:i, :d, :l, :r, true, :s)"),
					{"i": uuid.uuid4(), "d": f"cn={label}", "l": label, "r": role,
					 "s": server})
		assert alembic("upgrade", "head").returncode == 0
		assert roles() == ({"ops": "operator", "boss": "admin"},
		                   {"netops": "operator", "leads": "admin"})
		assert alembic("downgrade", "device_results_action_needed").returncode == 0
		assert roles() == ({"ops": "user", "boss": "admin"},
		                   {"netops": "user", "leads": "admin"})
	finally:
		engine.dispose()
		with admin.connect() as c:
			c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
		admin.dispose()
