"""Integration layer: real PostgreSQL + Redis, entry gated on service health.

Health: PostgreSQL and Redis are probed once at collection. Tests marked
`postgres` / `redis` (every test in this directory is, via `pytestmark`)
are skipped with the probe's reason when their service is down — `-ra`
lists them. Unit tests are unaffected.

Isolation (nothing here touches the developer's live data):
- PostgreSQL: a dedicated `rollout_test` database, dropped/created fresh per
  session, migrated to head via Alembic in a subprocess, truncated per test
- Redis: pinned to db 15 (hard assert), flushed per test
- Encryption: a throwaway key via env var; KEY_FILE already points at a
  temp dir (tests/conftest.py)
- Rollouts: orchestrator.submit is replaced for every test, so no test can
  reach netmiko; `captured_submits` records what would have run
- The app is created once per session: the Prometheus collector registers
  in a process-global registry, so a second create_app() would fail

Configure with env vars: TEST_PG_ADMIN_URL (a DB the test user can connect
to for CREATE/DROP DATABASE), TEST_PG_DBNAME, TEST_REDIS_URL.
"""
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis as redis_lib
from cryptography.fernet import Fernet
from dotenv import dotenv_values
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

import src.encryption as enc
from src.db import connections
from src.db.connections import PostgresConnection, PostgresConfig, RedisConnection, RedisConfig
from src.db.tables import Base, User, SecurityProfile, Inventory, VariableMapping
from src.encryption import decrypt, encrypt
from src.webapp.build import create_app
from src.webapp.hooks import conn_limit

ROOT = Path(__file__).resolve().parents[2]
REDIS_TEST_DB = 15
def _pg_admin_url() -> str:
	"""A superuser login (it creates and drops the test database): the dev
	stack's Postgres (compose.dev.yaml), with the superuser password from the
	repo's .env — unless TEST_PG_ADMIN_URL says otherwise (e.g. CI)."""
	url = os.environ.get("TEST_PG_ADMIN_URL")
	if url:
		return url
	password = dotenv_values(ROOT / ".env").get("POSTGRES_PASSWORD") or ""
	# 127.0.0.1, not localhost: the stack publishes IPv4 only, and on Windows
	# each refused ::1 attempt costs ~2 s per new connection
	return f"postgresql+psycopg2://postgres:{password}@127.0.0.1:5432/postgres"


PG_ADMIN_URL = _pg_admin_url()
TEST_DB = os.environ.get("TEST_PG_DBNAME", "rollout_test")
TEST_DB_URL = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + TEST_DB
TEST_PASSWORD = "Test-pass-1"


def _redis_url() -> str:
	"""TEST_REDIS_URL, else the app's Redis from config/runtime.env - always on db 15."""
	url = os.environ.get("TEST_REDIS_URL")
	if not url:
		# Same server the app uses (credentials from the developer's
		# config/runtime.env), never its db
		env = {k: v for k, v in dotenv_values(
			ROOT / "config" / "runtime.env").items()
		       if k.startswith("REDIS_")}
		url = env.get("REDIS_URL")
		if not url:
			password = env.get("REDIS_PASSWORD")
			auth = f":{password}@" if password else ""
			url = (f"redis://{auth}{env.get('REDIS_HOST', 'localhost')}:"
			       f"{env.get('REDIS_PORT', '6379')}/0")
	return re.sub(r"/\d+$", "", url) + f"/{REDIS_TEST_DB}"


REDIS_URL = _redis_url()


# ── Health probes (once, at collection) ──────────────────────────────────────

def _probe_postgres() -> str | None:
	"""None when the admin login connects, else why PostgreSQL is unreachable."""
	try:
		engine = create_engine(PG_ADMIN_URL, connect_args={"connect_timeout": 3})
		with engine.connect() as conn:
			conn.execute(text("SELECT 1"))
		engine.dispose()
		return None
	except Exception as e:
		host = PG_ADMIN_URL.split("@")[-1]
		return f"PostgreSQL unreachable at {host}: {type(e).__name__}"


def _probe_redis() -> str | None:
	"""None when the test Redis answers a ping, else why it is unreachable."""
	try:
		client = redis_lib.from_url(REDIS_URL, socket_connect_timeout=2,
		                            socket_timeout=2)
		client.ping()
		client.close()
		return None
	except Exception as e:
		host = REDIS_URL.split("@")[-1]
		return f"Redis unreachable at {host}: {type(e).__name__}"


PG_DOWN = _probe_postgres()
REDIS_DOWN = _probe_redis()


def pytest_collection_modifyitems(config, items):
	"""Skip tests marked postgres / redis / ldap, with the probe's reason, when
	that service is down."""
	for item in items:
		if item.get_closest_marker("postgres") and PG_DOWN:
			item.add_marker(pytest.mark.skip(reason=PG_DOWN))
		elif item.get_closest_marker("redis") and REDIS_DOWN:
			item.add_marker(pytest.mark.skip(reason=REDIS_DOWN))
		elif item.get_closest_marker("ldap") and LDAP_DOWN:
			item.add_marker(pytest.mark.skip(reason=LDAP_DOWN))


# ── Services ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def test_db_url():
	"""The scratch database's URL: rollout_test dropped and created fresh once per
	session, migrated to head, and dropped at the end."""
	if PG_DOWN:
		pytest.skip(PG_DOWN)
	admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
	with admin.connect() as conn:
		# WITH (FORCE): a crashed previous run may have left connections open
		conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
		conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
	# Alembic in a subprocess: in-process it would set DATABASE_URL and call
	# logging.fileConfig, disabling loggers for the rest of the session
	result = subprocess.run(
		[sys.executable, "-m", "alembic", "upgrade", "head"],
		cwd=ROOT / "src" / "db", capture_output=True, text=True,
		env=dict(os.environ, DATABASE_URL=TEST_DB_URL))
	assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
	yield TEST_DB_URL
	with admin.connect() as conn:
		conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
	admin.dispose()


@pytest.fixture(scope="session")
def redis_url():
	"""The test Redis URL (asserted to be db 15), flushed at the session's start
	and end."""
	if REDIS_DOWN:
		pytest.skip(REDIS_DOWN)
	client = redis_lib.from_url(REDIS_URL)
	assert client.connection_pool.connection_kwargs["db"] == REDIS_TEST_DB
	client.flushdb()
	yield REDIS_URL
	client.flushdb()
	client.close()


# Routes that raise genuine backend failures, for the error-handler tests
DEAD_PG_URL = "postgresql+psycopg2://nobody:nothing@127.0.0.1:5999/none"
UNROUTABLE_REDIS_HOST = "10.255.255.1"


def _register_failure_routes(app):
	"""Add /_test/<name> and /rollout/stream/_test/<name> routes that raise a real
	Postgres-down, Redis-timeout or bad-key error."""

	def pg_down():
		PostgresConnection(PostgresConfig(url=DEAD_PG_URL)).test_connection()

	def redis_timeout():
		# retries off: redis-py retries timeouts with backoff by default,
		# which turns one 1s timeout into ~20s
		redis_lib.Redis(host=UNROUTABLE_REDIS_HOST, socket_connect_timeout=1,
		                socket_timeout=1, retry=Retry(NoBackoff(), 0)).ping()

	def bad_key():
		decrypt("gAAAAA-not-a-real-token")

	for name, fn in (("pg_down", pg_down), ("redis_timeout", redis_timeout),
	                 ("bad_key", bad_key)):
		app.add_url_rule(f"/_test/{name}", f"_test_{name}", fn)
		app.add_url_rule(f"/rollout/stream/_test/{name}", f"_test_sse_{name}", fn)


@pytest.fixture(scope="session")
def app(test_db_url, redis_url, tmp_path_factory):
	"""The Flask app, created once per session on the scratch database and Redis
	db 15, with a throwaway encryption key and runtime.env, testing mode, no
	CSRF and the failure routes; its engine is disposed at the end."""


	os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
	scratch = tmp_path_factory.mktemp("backend")

	def _test_backend_init(self):
		self._CONFIG_ENV = scratch / "runtime.env"   # never the real one
		self.postgres = PostgresConnection(PostgresConfig(url=test_db_url))
		self.redis = RedisConnection(RedisConfig(url=redis_url))

	original_init = connections.BackendServices.__init__
	connections.BackendServices.__init__ = _test_backend_init
	try:
		flask_app = create_app()
	finally:
		connections.BackendServices.__init__ = original_init

	assert flask_app.backend.redis.client.connection_pool \
		       .connection_kwargs["db"] == REDIS_TEST_DB
	flask_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
	_register_failure_routes(flask_app)
	yield flask_app
	flask_app.backend.postgres.engine.dispose()


# ── Per-test state ───────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state(request):
	"""For tests using the app: every table truncated, Redis flushed and the
	connection limiter reset before the test; the app config put back after."""
	if "app" not in request.fixturenames:
		yield
		return
	app = request.getfixturevalue("app")
	tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
	with app.backend.postgres.engine.begin() as conn:
		# a leaked session holding locks fails this test instead of hanging
		conn.execute(text("SET LOCAL lock_timeout = '5s'"))
		conn.execute(text(f"TRUNCATE {tables} CASCADE"))
	app.backend.redis.client.flushdb()
	conn_limit.reset()
	saved_config = dict(app.config)
	yield
	app.config.clear()
	app.config.update(saved_config)


@pytest.fixture(autouse=True)
def _no_real_rollouts(request, monkeypatch):
	"""Route tests must never reach netmiko through the real orchestrator: its
	submit fails the test (use captured_submits)."""
	if "app" not in request.fixturenames:
		return
	app = request.getfixturevalue("app")

	def _refuse(*_, **__):
		raise AssertionError("real orchestrator.submit reached — use the "
		                     "captured_submits fixture")

	monkeypatch.setattr(app.orchestrator, "submit", _refuse)


@pytest.fixture(autouse=True)
def unreachable_targets(request, monkeypatch):
	"""Reachability probes never touch the network: every device is
	reachable unless a test adds its (ip, port) to this set."""
	down = set()
	if "app" in request.fixturenames:
		app = request.getfixturevalue("app")
		monkeypatch.setattr(app.web.reachability, "_probe",
		                    lambda ip, port: (ip, int(port)) not in down)
	return down


@pytest.fixture
def captured_submits(app, monkeypatch):
	"""The list of rollouts submitted during the test (one namespace each, with a
	new job id); nothing runs."""
	calls = []

	def _capture(devices, commands, params, user_id, comment=None):
		job_id = uuid.uuid4()
		calls.append(SimpleNamespace(devices=devices, commands=commands,
		                             params=params, user_id=user_id,
		                             comment=comment, job_id=job_id))
		return job_id

	monkeypatch.setattr(app.orchestrator, "submit", _capture)
	return calls


# ── Factories ────────────────────────────────────────────────────────────────

@pytest.fixture
def session_scope(app):
	"""The app's get_session: a database session context on the scratch database."""
	return app.backend.postgres.get_session


@pytest.fixture
def make_user(app):
	"""make_user(...) adds a user (approved, active operator with TEST_PASSWORD by
	default) and returns its id, username and role."""

	def _make(username=None, role="operator", approved=True, active=True, **kw):
		username = username or f"u_{uuid.uuid4().hex[:8]}"
		with app.backend.postgres.get_session() as s:
			user = User(username=username, role=role, is_approved=approved,
			            is_active=active,
			            password_hash=generate_password_hash(TEST_PASSWORD),
			            email=f"{username}@test.local", full_name=username, **kw)
			s.add(user)
			s.flush()
			return SimpleNamespace(id=user.id, username=username, role=role)

	return _make


@pytest.fixture
def make_profile(app):
	"""make_profile(owner, ...) adds a security profile (password encrypted) and
	returns its id."""

	def _make(owner, label="prof", username="netops", password="pw"):
		with app.backend.postgres.get_session() as s:
			p = SecurityProfile(label=label, username=username,
			                    password_secret=encrypt(password),
			                    user_id=owner.id)
			s.add(p)
			s.flush()
			return p.id

	return _make


@pytest.fixture
def make_device(app):
	"""make_device(owner, ...) adds an inventory device (cisco_ios on port 22 by
	default) and returns its id."""

	def _make(owner, ip="10.0.0.1", label=None, profile_id=None,
	          is_global=False, var_maps=None, device_type="cisco_ios", port=22):
		with app.backend.postgres.get_session() as s:
			d = Inventory(ip=ip, label=label or f"dev-{ip}", port=port,
			              device_type=device_type, user_id=owner.id,
			              sec_profile_id=profile_id, is_global=is_global,
			              var_maps=var_maps)
			s.add(d)
			s.flush()
			return d.id

	return _make


@pytest.fixture
def make_mapping(app):
	"""make_mapping(owner, token, prop, ...) adds a variable mapping ($$token$$ ->
	property) on the given devices and returns its id."""

	def _make(owner, token="HOST", prop="hostname", index=None, devices=()):
		with app.backend.postgres.get_session() as s:
			m = VariableMapping(token=f"$${token}$$", property_name=prop,
			                    index=index, user_id=owner.id)
			m.devices = [s.get(Inventory, d) for d in devices]
			s.add(m)
			s.flush()
			return m.id

	return _make


@pytest.fixture
def client_for(app):
	"""client_for(user=None, xhr=False): an HTTPS test client, signed in as `user`
	when given, sending the XHR header when asked."""
	def _client(user=None, xhr=False):
		client = app.test_client()
		client.environ_base["wsgi.url_scheme"] = "https"  # secure cookie
		if xhr:
			client.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
		if user is not None:
			with client.session_transaction() as sess:
				sess["_user_id"] = str(user.id)
				sess["_fresh"] = True
		return client

	return _client


@pytest.fixture
def db_get(app):
	"""Read a row back as a detached snapshot: db_get(Model, id)."""
	def _get(model, obj_id):
		with app.backend.postgres.get_session() as s:
			obj = s.get(model, obj_id)
			if obj is not None:
				s.expunge(obj)
			return obj

	return _get


# ── Ephemeral OpenLDAP directory (tests only — never part of the deployment) ─
#
# Started from the locally present `alpine` image with Alpine's OpenLDAP
# packages, seeded with an AD-shaped tree (users nested in OUs, a DN with a
# comma, a service account, a group), and force-removed at session end.
# Leftovers from a crashed run are removed at start (found by label).

LDAP_IMAGE = os.environ.get("TEST_LDAP_IMAGE", "alpine:latest")
LDAP_LABEL = "netrollout.test=ldap"
LDAP_BASE = "dc=corp,dc=test"
LDAP_ADMIN_DN, LDAP_ADMIN_PW = f"cn=admin,{LDAP_BASE}", "adminpw"
LDAP_SERVICE_DN, LDAP_SERVICE_PW = f"cn=svc,ou=Service,{LDAP_BASE}", "svc-pass"
LDAP_GROUP_DN = f"cn=netops,ou=Groups,{LDAP_BASE}"
LDAP_USERS = {  # uid -> (dn, password)
	"jdoe": (f"cn=John Doe,ou=Network,ou=Users,{LDAP_BASE}", "jdoe-pass"),
	"bsmith": (rf"cn=Smith\, Bob,ou=Users,{LDAP_BASE}", "bob-pass"),
	"alice": (f"cn=Alice Brown,ou=Users,{LDAP_BASE}", "alice-pass"),
}

_SLAPD_CONF = f"""include /etc/openldap/schema/core.schema
include /etc/openldap/schema/cosine.schema
include /etc/openldap/schema/inetorgperson.schema
pidfile /tmp/slapd.pid
modulepath /usr/lib/openldap
moduleload back_mdb.so
database mdb
maxsize 10485760
suffix "{LDAP_BASE}"
rootdn "{LDAP_ADMIN_DN}"
rootpw {LDAP_ADMIN_PW}
directory /tmp/ldapdata
access to attrs=userPassword by self write by anonymous auth by * none
access to * by users read by * none
"""

_SEED_LDIF = f"""dn: {LDAP_BASE}
objectClass: dcObject
objectClass: organization
dc: corp
o: Corp

dn: ou=Users,{LDAP_BASE}
objectClass: organizationalUnit
ou: Users

dn: ou=Network,ou=Users,{LDAP_BASE}
objectClass: organizationalUnit
ou: Network

dn: ou=Service,{LDAP_BASE}
objectClass: organizationalUnit
ou: Service

dn: ou=Groups,{LDAP_BASE}
objectClass: organizationalUnit
ou: Groups

dn: {LDAP_USERS["jdoe"][0]}
objectClass: inetOrgPerson
cn: John Doe
sn: Doe
uid: jdoe
mail: jdoe@corp.test
displayName: John Doe
userPassword: {LDAP_USERS["jdoe"][1]}

dn: {LDAP_USERS["bsmith"][0]}
objectClass: inetOrgPerson
cn: Smith, Bob
sn: Smith
uid: bsmith
userPassword: {LDAP_USERS["bsmith"][1]}

dn: {LDAP_USERS["alice"][0]}
objectClass: inetOrgPerson
cn: Alice Brown
sn: Brown
uid: alice
userPassword: {LDAP_USERS["alice"][1]}

dn: cn=Dup One,ou=Users,{LDAP_BASE}
objectClass: inetOrgPerson
cn: Dup One
sn: One
uid: dup
userPassword: dup-pass

dn: cn=Dup Two,ou=Network,ou=Users,{LDAP_BASE}
objectClass: inetOrgPerson
cn: Dup Two
sn: Two
uid: dup
userPassword: dup-pass

dn: {LDAP_SERVICE_DN}
objectClass: inetOrgPerson
cn: svc
sn: svc
userPassword: {LDAP_SERVICE_PW}

dn: {LDAP_GROUP_DN}
objectClass: groupOfNames
cn: netops
member: {LDAP_USERS["jdoe"][0]}
member: {LDAP_USERS["bsmith"][0]}
"""


def _docker(*args, **kw):
	return subprocess.run(["docker", *args], capture_output=True, text=True,
	                      timeout=kw.pop("timeout", 60), **kw)


def _probe_ldap() -> str | None:
	"""None when Docker runs and the LDAP image is present locally, else why not."""
	try:
		if _docker("info", "--format", "{{.ServerVersion}}",
		           timeout=10).returncode != 0:
			return "Docker daemon unavailable (needed for the test directory)"
		if _docker("image", "inspect", LDAP_IMAGE, timeout=10).returncode != 0:
			return f"Docker image {LDAP_IMAGE} not present locally"
		return None
	except (FileNotFoundError, subprocess.TimeoutExpired) as e:
		return f"Docker unavailable: {type(e).__name__}"


LDAP_DOWN = _probe_ldap()


def _remove_leftover_ldap_containers():
	ids = _docker("ps", "-aq", "--filter", f"label={LDAP_LABEL}").stdout.split()
	if ids:
		_docker("rm", "-f", *ids)


@pytest.fixture(scope="session")
def ldap_directory():
	"""Ephemeral OpenLDAP server; yields its host port. Removed afterwards."""
	if LDAP_DOWN:
		pytest.skip(LDAP_DOWN)
	_remove_leftover_ldap_containers()
	run = _docker(
		"run", "-d", "--rm", "--label", LDAP_LABEL, "-p", "127.0.0.1::389",
		"-e", f"SLAPD_CONF={_SLAPD_CONF}", LDAP_IMAGE, "sh", "-c",
		"apk add --no-cache -q openldap openldap-back-mdb openldap-clients "
		">/dev/null && mkdir -p /tmp/ldapdata && "
		"printf '%s' \"$SLAPD_CONF\" > /tmp/slapd.conf && "
		"exec slapd -f /tmp/slapd.conf -h ldap:/// -d 0", timeout=60)
	if run.returncode != 0:
		pytest.skip(f"test directory failed to start: {run.stderr.strip()[:200]}")
	container = run.stdout.strip()
	try:
		whoami = ("exec", container, "ldapwhoami", "-x", "-H", "ldap://127.0.0.1",
		          "-D", LDAP_ADMIN_DN, "-w", LDAP_ADMIN_PW)
		deadline = time.time() + 120  # includes the apk install
		while _docker(*whoami, timeout=10).returncode != 0:
			if time.time() > deadline or not _docker(
					"ps", "-q", "--filter", f"id={container}").stdout.strip():
				logs = _docker("logs", container).stderr[-300:]
				pytest.skip(f"test directory never became ready: {logs}")
			time.sleep(1)
		seeded = _docker("exec", "-i", container, "ldapadd", "-x", "-H",
		                 "ldap://127.0.0.1", "-D", LDAP_ADMIN_DN, "-w",
		                 LDAP_ADMIN_PW, input=_SEED_LDIF)
		assert seeded.returncode == 0, f"seeding failed: {seeded.stderr}"
		port = int(_docker("port", container, "389/tcp").stdout.strip()
		           .rsplit(":", 1)[1])
		yield port
	finally:
		_docker("rm", "-f", container)


@pytest.fixture
def ldap_server_config(ldap_directory):
	"""An LDAPServer-shaped object for the test directory (search-then-bind
	via the service account). Needs encryption initialised."""
	return SimpleNamespace(
		host="127.0.0.1", port=ldap_directory, use_ssl=False,
		base_dn=LDAP_BASE, cn_identifier="uid", bind_type="regular",
		bind_dn=LDAP_SERVICE_DN, bind_password=encrypt(LDAP_SERVICE_PW))
