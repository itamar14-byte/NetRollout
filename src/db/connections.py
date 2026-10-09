"""NetRollout's connections: Postgres (PostgresConfig / PostgresConnection),
Redis (RedisConfig / RedisConnection) - both ServiceConnections: where they
point, bundled or an organisation's, switching live - config/runtime.env
(RuntimeEnv), and BackendServices - both, the settings store, the audit
trail, a database move's and a Redis switch's entry points. Where they point
is resolved from config/runtime.env over the environment."""
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import ClassVar, Generic, TypeVar
from urllib.parse import quote

import redis
from dotenv import dotenv_values, load_dotenv
from redis.backoff import NoBackoff
from redis.connection import parse_url
from redis.retry import Retry
from sqlalchemy import URL, create_engine, make_url, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from src import runtime
from src.audit import AuditTrail
from src.db.install import install, install_extras
from src.db.settings import SettingsStore
from src.db.tables import SecurityProfile, LDAPServer, User


# The schema names NetRollout uses: plain lowercase identifiers, which need no
# quoting (the schema goes unquoted into the connection's search_path)
SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SCHEMA_RULE = ("lowercase letters, digits and _ only, starting with a letter or _, "
               "at most 63 characters")


class ServiceMode(StrEnum):
	"""Whether NetRollout uses a bundled service or an organisation's
	(ServiceConnection.mode, BackendServices.connection_modes)."""
	BUNDLED = "bundled"
	EXTERNAL = "external"


# Hosts of the bundled services: local development, or the compose service
# names (deploy/compose.yaml must use these names)
BUNDLED_HOSTS = {"POSTGRES": ("localhost", "127.0.0.1", "postgres"),
                 "REDIS": ("localhost", "127.0.0.1", "redis")}
# runtime.env: the bundled database's connection, kept when a move leaves it
# (the bundled Postgres keeps running, untouched) - Move back goes there
BUNDLED_DATABASE_KEY = "NETROLLOUT_BUNDLED_DATABASE_URL"
# the same for Redis, kept by a switch away from the bundled one
BUNDLED_REDIS_KEY = "NETROLLOUT_BUNDLED_REDIS_URL"
BUNDLED_KEYS = {"POSTGRES": BUNDLED_DATABASE_KEY, "REDIS": BUNDLED_REDIS_KEY}
# runtime.env: whether Grafana connects with TLS - as the app's own connection
# does (deploy/grafana/setup.py reads it with the connection)
GRAFANA_SSLMODE_KEY = "NETROLLOUT_GRAFANA_SSLMODE"


def load_config(path: Path, override: bool = True) -> None:
	"""config/runtime.env into the environment, literally: a password's `$`
	isn't expanded.

	:param path: the file (a missing one changes nothing)
	:param override: whether it wins over the environment"""
	load_dotenv(path, override=override, interpolate=False)


def _escaped(value: str) -> str:
	""":returns: the value for a single-quoted runtime.env line"""
	return value.replace("\\", "\\\\").replace("'", "\\'")


class RuntimeEnv:
	"""config/runtime.env: the app-owned settings file - only what Server
	Management wrote (a database move, a Redis switch), loaded over the
	environment - and what the switches remember there (the bundled
	services' addresses)."""

	def __init__(self, path: Path) -> None:
		""":param path: the file (it may not exist yet)"""
		self.path = path

	def load(self) -> None:
		"""Into the environment, over it (load_config)."""
		load_config(self.path)

	def values(self) -> dict[str, str | None]:
		""":returns: the file's keys ({} when there's no file)"""
		return (dict(dotenv_values(self.path, interpolate=False))
		        if self.path.exists() else {})

	def merge(self, updates: dict[str, str]) -> None:
		"""Merge `updates` into the file - atomically (a crash mid-write can't
		leave half a file) and owner-only (it holds database and Redis
		passwords)."""
		cfg = self.values()
		cfg.update(updates)
		self.path.parent.mkdir(parents=True, exist_ok=True)
		tmp = self.path.with_name(self.path.name + ".tmp")
		# single-quoted, \ and ' escaped: a password may hold any character
		tmp.write_text("".join(f"{k}='{_escaped(v or '')}'\n" for k, v in cfg.items()),
		               encoding="utf-8")
		os.chmod(tmp, 0o600)
		os.replace(tmp, self.path)

	def remembered_bundled(self, service: str) -> str | None:
		""":param service: "POSTGRES" or "REDIS"
		:returns: the bundled service's URL, kept by the move / switch that
		 left it; None: none remembered"""
		return self.values().get(BUNDLED_KEYS[service]) or None


def schema_problem(schema: str) -> str | None:
	""":returns: why NetRollout can't use this schema name (in words), else None"""
	if SCHEMA_RE.match(schema):
		return None
	return f'The schema name "{schema}" can\'t be used: {SCHEMA_RULE}.'


@dataclass(frozen=True)
class PostgresConfig:
	"""Where the database is: a URL, or host / port / database / login, and
	the schema NetRollout's tables live in (`public` when None)."""
	host: str = "localhost"
	port: str = "5432"
	database: str = "rollout_db"
	user: str = "dbadmin"
	password: str = "Pass123"
	schema: str | None  = None
	url: str | None = None      # wins over the parts when set

	@classmethod
	def unload_env(cls) -> "PostgresConfig":
		"""The settings from the environment (DATABASE_URL, else PG_HOST /
		_PORT / _NAME / _USER / _PASSWORD / _SCHEMA), with the defaults for
		what's missing.

		:raises ValueError: PG_SCHEMA isn't a name NetRollout can use (blank:
		 no schema)"""
		# lower-cased as Postgres folds an unquoted name: an install whose
		# PG_SCHEMA=MySchema always used the schema myschema
		schema = os.getenv("PG_SCHEMA")
		if schema:
			schema = schema.lower()
		if schema and (problem := schema_problem(schema)):
			raise ValueError(f"PG_SCHEMA: {problem}")
		return cls (
			os.getenv("PG_HOST", "localhost"),
			os.getenv("PG_PORT", "5432"),
			os.getenv("PG_NAME", "rollout_db"),
			os.getenv("PG_USER", "dbadmin"),
			os.getenv("PG_PASSWORD", "Pass123"),
			schema,
			os.getenv("DATABASE_URL")
		)

	def get_url(self) -> str:
		""":returns: the SQLAlchemy URL to connect with (the password in it)"""
		if self.url:
			return self.url
		# URL.create escapes: a password with @ / : # stays one password
		return URL.create("postgresql+psycopg2", username=self.user,
		                  password=self.password, host=self.host,
		                  port=int(self.port), database=self.database
		                  ).render_as_string(hide_password=False)

	def place(self) -> tuple[str | None, int, str | None, str]:
		"""Where the data is - server, port, database, schema - whatever the
		login: two configs with the same place are the same database."""
		url = make_url(self.get_url())
		return (url.host, url.port or 5432, url.database, self.schema or "public")

	def describe(self) -> str:
		"""host:port/database[ (schema s)], no password - for pages and the audit."""
		url = make_url(self.get_url())
		place = f"{url.host}:{url.port or 5432}/{url.database}"
		return place + (f" (schema {self.schema})" if self.schema else "")

	def to_env_dict(self) -> dict[str, str]:
		"""The settings as config/runtime.env keys - every key, blank rather
		than absent: runtime.env is loaded over the container environment, and
		an inherited DATABASE_URL or PG_SCHEMA would otherwise still apply
		after a switch."""
		return {
			"PG_HOST": self.host,
			"PG_PORT": self.port,
			"PG_NAME": self.database,
			"PG_USER": self.user,
			"PG_PASSWORD": self.password,
			"PG_SCHEMA": self.schema or "",
			"DATABASE_URL": "",
		}

Config = TypeVar("Config", "PostgresConfig", "RedisConfig")


class ServiceConnection(ABC, Generic[Config]):
	"""A connection the app holds to one service (Postgres or Redis): where
	it points, whether that's the bundled service or an organisation's
	(mode), the bundled one's address, and a live switch elsewhere
	(switch_to) - the steps that differ are the subclasses' hooks."""
	SERVICE: ClassVar[str]          # "POSTGRES" / "REDIS" (BUNDLED_HOSTS, BUNDLED_KEYS)
	URL_KEY: ClassVar[str]          # runtime.env's URL key (DATABASE_URL / REDIS_URL)
	config: Config

	@staticmethod
	@abstractmethod
	def config_from_url(url: str) -> Config:
		""":returns: a config for this service's URL"""

	@abstractmethod
	def live_host(self) -> str | None:
		""":returns: the host the live connection uses (with a URL, the
		 config's host is only the default)"""

	@abstractmethod
	def own_url(self) -> str:
		""":returns: the URL of where it points now, password included (what
		 a switch away from the bundled service remembers)"""

	@abstractmethod
	def swap(self, config: Config) -> None:
		"""The live connection replaced (switch_to's first step).

		:raises RuntimeError: the new server doesn't answer - nothing changed"""

	def place(self) -> tuple[object, ...]:
		""":returns: where the data is, whatever the login (the config's place)"""
		return self.config.place()

	def describe(self) -> str:
		""":returns: where it points, no password - for pages and the audit"""
		return self.config.describe()

	def mode(self, env: RuntimeEnv) -> ServiceMode:
		"""Bundled or an organisation's: by host (the compose names,
		localhost), or by the whole place once a move or switch has
		remembered the bundled one's (another database on the same host isn't
		the bundled one).

		:param env: runtime.env, where the bundled address is remembered"""
		remembered = env.remembered_bundled(self.SERVICE)
		if remembered:
			return (ServiceMode.BUNDLED if self.config_from_url(remembered).place()
			        == self.place() else ServiceMode.EXTERNAL)
		return (ServiceMode.BUNDLED if self.live_host() in BUNDLED_HOSTS[self.SERVICE]
		        else ServiceMode.EXTERNAL)

	def bundled_config(self, env: RuntimeEnv) -> Config | None:
		"""The bundled service: remembered by the switch that left it, or the
		current one while on it; None if unknown (switched by hand before
		switches remembered it)."""
		remembered = env.remembered_bundled(self.SERVICE)
		if remembered:
			return self.config_from_url(remembered)
		if self.mode(env) == ServiceMode.BUNDLED:
			return self.config
		return None

	def switch_to(self, config: Config, env: RuntimeEnv) -> None:
		"""A live switch: the connection replaced (swap) - every user of it
		looks the current one up - then runtime.env: every key of the service
		(a URL: the parts blank), and leaving the bundled service its address
		first, for the way back. A failure after the swap goes to undo().

		:param env: runtime.env, written last
		:raises RuntimeError: the new server doesn't answer (nothing changed)"""
		leaving = self.own_url() if self.mode(env) == ServiceMode.BUNDLED else None
		old = self.config
		self.swap(config)
		try:
			updates = config.to_env_dict()
			if config.url:          # the parts blank: the URL is the connection
				updates.update({k: "" for k in updates}, **{self.URL_KEY: config.url})
			if leaving:
				updates[BUNDLED_KEYS[self.SERVICE]] = leaving
			self.after_swap(updates)
			env.merge(updates)
		except Exception as e:
			self.undo(old, e)
			raise

	def after_swap(self, updates: dict[str, str]) -> None:
		"""What the new connection needs before runtime.env names it (none).

		:param updates: runtime.env's new keys - more may be added"""

	def undo(self, old: Config, error: Exception) -> None:
		"""A step after the swap failed: put things back (nothing - the error
		is raised as it is).

		:param old: where it pointed before"""


class PostgresConnection(ServiceConnection[PostgresConfig]):
	"""The app's engine and sessions, replaceable live (reload_db)."""
	SERVICE = "POSTGRES"
	URL_KEY = "DATABASE_URL"

	def __init__(self, config: PostgresConfig | None = None):
		""":param config: where the database is; the environment's settings
		 when None"""
		self.config = config or PostgresConfig.unload_env()
		self.engine = self.build_engine(self.config)
		self.session_factory = sessionmaker(bind=self.engine)

	@staticmethod
	def build_engine(config: PostgresConfig) -> Engine:
		"""An engine for `config`, its schema first on the search path;
		connections are checked before use (pool_pre_ping) - a restarted
		server doesn't hand out dead ones."""
		connect_args = {"options": f"-c search_path={config.schema}"} if (
			config.schema) else {}
		return create_engine(config.get_url(), connect_args=connect_args,
		                     pool_pre_ping=True)


	@contextmanager
	def get_session(self) -> Iterator[Session]:
		"""A session for one unit of work: committed when the block ends,
		rolled back (and the error raised) when it fails, closed either way."""
		session = self.session_factory()
		try:
			yield session
			session.commit()
		except Exception:
			session.rollback()
			raise
		finally:
			session.close()

	def test_connection(self) -> bool:
		""":returns: True when the database answers
		:raises sqlalchemy.exc.OperationalError: it doesn't"""
		with self.get_session() as conn:
			conn.execute(text("SELECT 1"))
		return True

	def reload_db(self, config: PostgresConfig | None = None,
	              install_flag: bool = True) -> None:
		"""Switch to another database live: the new engine is checked first,
		then swapped in (sessions follow) and the old one disposed.

		:param config: the new database; the environment's settings when None
		:param install_flag: run install() on it (migrations, seeds); a
		 database move passes False - its copy brings everything
		:raises RuntimeError: the new server doesn't answer - nothing changed"""
		new_config = config or PostgresConfig.unload_env()
		new_engine = self.build_engine(new_config)

		try:
			with new_engine.connect() as conn:
				conn.execute(text("SELECT 1"))
		except OperationalError:
			raise RuntimeError("New server unavailable")

		old_engine = self.engine

		self.config = new_config
		self.engine = new_engine
		self.session_factory.configure(bind=new_engine)

		old_engine.dispose()

		if install_flag:
			install(self)

	def disconnect(self) -> None:
		"""Close the engine's pooled connections."""
		self.engine.dispose()

	# ── ServiceConnection's hooks ──
	@staticmethod
	def config_from_url(url: str) -> PostgresConfig:
		return PostgresConfig(url=url)

	def live_host(self) -> str | None:
		return self.engine.url.host

	def own_url(self) -> str:
		return self.engine.url.render_as_string(hide_password=False)

	def swap(self, config: PostgresConfig) -> None:
		"""reload_db without install(): it would seed the factory admin into a
		copy whose admins renamed or removed it."""
		self.reload_db(config, install_flag=False)

	def after_swap(self, updates: dict[str, str]) -> None:
		"""Grafana's grants on the new database (install_extras), and whether
		Grafana connects with TLS - as the app does."""
		install_extras(self)
		updates[GRAFANA_SSLMODE_KEY] = "require" if self._uses_tls() else "disable"

	def undo(self, old: PostgresConfig, error: Exception) -> None:
		"""Back on the old database.

		:raises RuntimeError: always - the error, and whether the way back
		 failed too"""
		try:
			self.reload_db(old, install_flag=False)
		except RuntimeError:
			raise RuntimeError(f"{error}; the old database didn't answer when switching "
			                   f"back either - restart NetRollout (runtime.env still "
			                   f"names the old one)") from error
		raise RuntimeError(str(error)) from error

	def _uses_tls(self) -> bool:
		""":returns: whether the app's own database connection is encrypted
		 (Grafana's then is too); False when it can't be asked"""
		try:
			with self.engine.connect() as conn:
				return bool(conn.execute(text(
					"SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")).scalar())
		except SQLAlchemyError:
			return False


# Client timeouts (seconds): fail fast when Redis is unreachable
CONNECT_TIMEOUT = 3
SOCKET_TIMEOUT = 15  # > orchestration._BLPOP_TIMEOUT (enforced by a test)

# "Redis is unavailable": a refusing host raises ConnectionError, an
# unreachable one raises TimeoutError — which is NOT a ConnectionError
# subclass. Catch both wherever Redis being down should be survivable.
REDIS_UNAVAILABLE = (redis.exceptions.ConnectionError,
					 redis.exceptions.TimeoutError)

@dataclass(frozen=True)
class RedisConfig:
	"""Where Redis is: a URL, or host / port / database number / password."""
	host: str = "localhost"
	port: str = "6379"
	db: str = "0"
	password: str | None = None
	url: str | None = None      # wins over the parts when set

	@classmethod
	def unload_env(cls) -> "RedisConfig":
		"""The settings from the environment (REDIS_URL, else REDIS_HOST /
		_PORT / _DB / _PASSWORD), with the defaults for what's missing."""
		return cls(
			os.getenv("REDIS_HOST", "localhost"),
			os.getenv("REDIS_PORT", "6379"),
			os.getenv("REDIS_DB", "0"),
			os.getenv("REDIS_PASSWORD", ""),
			os.getenv("REDIS_URL")
		)

	def get_url(self) -> str:
		""":returns: the redis:// URL to connect with (the password in it)"""
		if self.url:
			return self.url
		if self.password:      # URL-encoded: any character may be in it
			return f"redis://:{quote(self.password, safe='')}@{self.host}:{self.port}/{self.db}"
		return f"redis://{self.host}:{self.port}/{self.db}"

	def place(self) -> tuple[str | None, int, int]:
		"""Which Redis: server, port, database number, whatever the password."""
		parts = parse_url(self.get_url())
		return (parts.get("host"), int(parts.get("port") or 6379), int(parts.get("db") or 0))

	def describe(self) -> str:
		"""host:port/db, no password - for pages and the audit."""
		return "{}:{}/{}".format(*self.place())

	def to_env_dict(self) -> dict[str, str]:
		"""The settings as config/runtime.env keys - every key, blank rather
		than absent: runtime.env is loaded over the container environment, and
		an inherited REDIS_URL or REDIS_PASSWORD would otherwise still apply
		after a switch."""
		return {
			"REDIS_HOST": self.host,
			"REDIS_PORT": self.port,
			"REDIS_DB": self.db or "0",
			"REDIS_PASSWORD": self.password or "",
			"REDIS_URL": "",
		}



class RedisConnection(ServiceConnection[RedisConfig]):
	"""The app's Redis client, replaceable live (reload_db)."""
	SERVICE = "REDIS"
	URL_KEY = "REDIS_URL"

	def __init__(self, config: RedisConfig | None = None):
		""":param config: where Redis is; the environment's settings when None"""
		self.config = config or RedisConfig.unload_env()
		self.client = self._build_client(self.config)

	@staticmethod
	def _build_client(config: RedisConfig) -> redis.Redis:
		"""A client with short timeouts and one retry, not connected yet."""
		# Without explicit timeouts an unreachable (silent) host costs the OS
		# connect timeout on every attempt, times redis-py's default retries
		# — ~20s per request. SOCKET_TIMEOUT must stay above the
		# dispatcher's BLPOP wait, which blocks by design.
		return redis.from_url(config.get_url(),
							  socket_connect_timeout=CONNECT_TIMEOUT,
							  socket_timeout=SOCKET_TIMEOUT,
							  retry=Retry(NoBackoff(), 1))

	def test_connection(self) -> bool:
		""":returns: True when Redis answers a PING
		:raises redis.exceptions.ConnectionError: refused
		:raises redis.exceptions.TimeoutError: no answer (see REDIS_UNAVAILABLE)"""
		self.client.ping()
		return True

	def reload_db(self, config: RedisConfig | None = None) -> None:
		"""Switch to another Redis live: the new client is checked first, then
		swapped in and the old one closed.

		:param config: the new Redis; the environment's settings when None
		:raises RuntimeError: the new server refused or didn't answer -
		 nothing changed"""
		new_config = config or RedisConfig.unload_env()
		new_client = self._build_client(new_config)

		try:
			new_client.ping()
		except REDIS_UNAVAILABLE:      # refused, or no answer (a TimeoutError)
			raise RuntimeError("New server unavailable")

		old_client = self.client
		self.config = new_config
		self.client = new_client

		old_client.close()

	def disconnect(self) -> None:
		"""Close the client's connections (it reconnects when used again)."""
		self.client.close()

	# ── ServiceConnection's hooks ──
	@staticmethod
	def config_from_url(url: str) -> RedisConfig:
		return RedisConfig(url=url)

	def live_host(self) -> str | None:
		return self.client.connection_pool.connection_kwargs.get("host")

	def own_url(self) -> str:
		return self.config.get_url()

	def swap(self, config: RedisConfig) -> None:
		self.reload_db(config)


# Every column holding Fernet ciphertext
ENCRYPTED_COLUMNS = (SecurityProfile.password_secret,
                      SecurityProfile.enable_secret,
                      LDAPServer.bind_password,
                      User.otp_secret)
# All Fernet tokens start with this (version byte 0x80, base64-encoded)
FERNET_PREFIX = "gAAAAA"


class BackendServices:
	"""PostgreSQL, Redis and the settings, one of each per process."""

	def __init__(self, env: RuntimeEnv | None = None) -> None:
		"""Connect, bring the database up to this version (install), and
		connect Redis. The settings come from config/runtime.env (only what
		Server Management wrote: a database move, a Redis switch), which wins
		over the environment (the installer's .env), which wins over the code's
		defaults.

		:param env: runtime.env; the app's (config/runtime.env) when None"""
		self.env = env or RuntimeEnv(runtime.runtime_env())
		self.env.load()
		self.postgres = PostgresConnection()
		install(self.postgres)
		self.redis = RedisConnection()

	@cached_property
	def settings(self) -> SettingsStore:
		"""The System Settings - their connection looked up per call, so a
		database move is followed."""
		return SettingsStore(lambda: self.postgres)

	@cached_property
	def audit_trail(self) -> AuditTrail:
		"""The audit log's writer - its connection looked up per row, so a
		database move is followed."""
		return AuditTrail(lambda: self.postgres)

	def health(self) -> dict[str, bool]:
		""":returns: {"POSTGRES": up, "REDIS": up} - each asked now, a failure
		 caught (never raises)"""
		try:
			postgres_up = self.postgres.test_connection()
		except OperationalError:
			postgres_up = False

		try:
			redis_up = self.redis.test_connection()
		except REDIS_UNAVAILABLE:
			redis_up = False
		return {
			"POSTGRES": postgres_up,
			"REDIS": redis_up
		}

	def encrypted_sample(self) -> str | None:
		"""One stored Fernet token from any encrypted column, or None if the
		DB holds no encrypted data. Only Fernet-shaped values are considered,
		so a legacy plaintext value can't fail the startup key check.
		:raises OperationalError: Postgres unreachable"""
		with self.postgres.get_session() as db_session:
			for column in ENCRYPTED_COLUMNS:
				value = db_session.query(column).filter(
					column.like(f"{FERNET_PREFIX}%")).limit(1).scalar()
				if value:
					return value
		return None

	def connection_modes(self) -> dict[str, ServiceMode]:
		"""Whether NetRollout uses the bundled services or an organisation's.

		:returns: {"POSTGRES": a ServiceMode, "REDIS": ...} (ServiceConnection.mode)"""
		return {"POSTGRES": self.postgres.mode(self.env),
		        "REDIS": self.redis.mode(self.env)}

	def bundled_postgres(self) -> PostgresConfig | None:
		"""The bundled database's connection: remembered by the move that left
		it, or the current one while on it; None if unknown (the database was
		switched by hand before moves existed)."""
		return self.postgres.bundled_config(self.env)

	def move_postgres(self, config: PostgresConfig) -> None:
		"""The switch at the end of a database move (src/webapp/db_move.py):
		the connection replaced live (Grafana's grants on the new one:
		install_extras), then runtime.env - leaving the bundled database,
		its connection is kept there first, for Move back. Anything failing
		after the reconnect puts the connection back on the old database.
		:raises RuntimeError: the new server doesn't answer, or a step after
		 it failed - either way NetRollout is on its old database, runtime.env
		 unchanged"""
		self.postgres.switch_to(config, self.env)

	def bundled_redis(self) -> RedisConfig | None:
		"""The bundled Redis: remembered by the switch that left it, or the
		current one while on it; None if unknown."""
		return self.redis.bundled_config(self.env)

	def reload_redis(self, config: RedisConfig) -> None:
		"""A live switch (Server Management): the client replaced - every user
		of it looks the current one up - then runtime.env; leaving the bundled
		Redis, its address is kept there first, for the way back.
		:raises RuntimeError: the new server doesn't answer (nothing changed)"""
		self.redis.switch_to(config, self.env)
