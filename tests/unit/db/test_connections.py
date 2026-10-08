"""The connections (src/db/connections.py): Redis reconnects and timeouts,
where Postgres / Redis settings come from (runtime.env over the environment),
switch writes, connection modes and the bundled database's address."""
import os
import socket
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import redis
from dotenv import dotenv_values, load_dotenv
from sqlalchemy.engine import make_url

from src import jobs
from src.db import connections
from src.db.connections import (RedisConfig, RedisConnection, CONNECT_TIMEOUT, REDIS_UNAVAILABLE,
                                SOCKET_TIMEOUT, BackendServices, PostgresConfig)


class FakeClient:
	"""Stands in for a redis.Redis: its PING fails as told, or answers."""
	def __init__(self, fails_with: Exception | None = None):
		self.fails_with, self.closed = fails_with, False

	def ping(self) -> bool:
		if self.fails_with:
			raise self.fails_with
		return True

	def close(self) -> None:
		self.closed = True


@pytest.mark.parametrize("failure", [
	redis.exceptions.ConnectionError("refused"),     # the server says no
	redis.exceptions.TimeoutError("no answer"),      # a silent host: not a ConnectionError
])
def test_an_unreachable_server_is_refused_in_words_and_nothing_changes(monkeypatch, failure):
	"""A new server whose PING fails (refused, or a timeout) makes reload_db raise
	"New server unavailable"; the current client stays, open, with its config."""
	current = FakeClient()
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: current))
	conn = RedisConnection(RedisConfig(host="redis"))
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: FakeClient(failure)))

	with pytest.raises(RuntimeError, match="New server unavailable"):
		conn.reload_db(RedisConfig(host="cache.example.org"))
	assert conn.client is current and not current.closed
	assert conn.config.host == "redis"


def test_a_reachable_server_replaces_the_client(monkeypatch):
	"""A reachable new server replaces the client and config; the old client is
	closed."""
	old, new = FakeClient(), FakeClient()
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: old))
	conn = RedisConnection(RedisConfig(host="redis"))
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: new))
	conn.reload_db(RedisConfig(host="cache.example.org"))
	assert conn.client is new and old.closed and conn.config.host == "cache.example.org"


def test_both_failures_count_as_unavailable():
	"""REDIS_UNAVAILABLE includes both TimeoutError and ConnectionError."""
	assert redis.exceptions.TimeoutError in connections.REDIS_UNAVAILABLE
	assert redis.exceptions.ConnectionError in connections.REDIS_UNAVAILABLE


def test_client_is_built_with_timeouts():
	"""The client's connections use CONNECT_TIMEOUT and SOCKET_TIMEOUT."""
	kwargs = RedisConnection(RedisConfig()).client.connection_pool.connection_kwargs
	assert kwargs["socket_connect_timeout"] == CONNECT_TIMEOUT
	assert kwargs["socket_timeout"] == SOCKET_TIMEOUT


def test_socket_timeout_exceeds_dispatcher_blpop_wait():
	"""SOCKET_TIMEOUT is longer than the dispatcher's BLPOP wait: the socket
	timeout applies to BLPOP too, and if it were shorter every idle dispatcher
	wait would raise instead of returning None."""
	assert SOCKET_TIMEOUT > jobs._BLPOP_TIMEOUT


class SilentHostSocket:
	"""Stands in for socket.socket towards a host that drops every packet: each
	connect "waits" the timeout set on it (counted, not slept) and times out."""
	connects: list[float] = []

	def __init__(self, *args):
		self.timeout = None

	def setsockopt(self, *args):
		pass

	def settimeout(self, timeout):
		self.timeout = timeout

	def connect(self, address):
		SilentHostSocket.connects.append(self.timeout)
		raise socket.timeout("timed out")

	def shutdown(self, how):
		pass

	def close(self):
		pass


def test_unreachable_host_fails_fast(monkeypatch):
	"""A PING to a host that never answers raises a REDIS_UNAVAILABLE error after at
	most two connect attempts (one retry), each bounded by CONNECT_TIMEOUT: within
	2 x CONNECT_TIMEOUT, never the ~20 s it used to take. No network: the socket is
	a stand-in that times out, and the address is numeric (no DNS)."""
	SilentHostSocket.connects = []
	fake_socket = SimpleNamespace(**{name: getattr(socket, name) for name in dir(socket)
	                                 if not name.startswith("__")})
	fake_socket.socket = SilentHostSocket
	monkeypatch.setattr(redis.connection, "socket", fake_socket)
	client = RedisConnection(RedisConfig(host="192.0.2.1")).client    # TEST-NET-1
	start = time.monotonic()
	with pytest.raises(REDIS_UNAVAILABLE):
		client.ping()
	assert time.monotonic() - start < 1                       # nothing really waited
	attempts = SilentHostSocket.connects
	assert 1 <= len(attempts) <= 2 and set(attempts) == {CONNECT_TIMEOUT}
	assert sum(attempts) <= 2 * CONNECT_TIMEOUT


# ── Config precedence ────────────────────────────────────────────────────────

@pytest.fixture
def isolated_env(monkeypatch):
	"""The monkeypatch with every Postgres / Redis connection variable unset; the
	whole os.environ is restored afterwards, as load_dotenv writes it directly."""
	saved = dict(os.environ)
	for key in ("DATABASE_URL", "PG_HOST", "PG_PORT", "PG_NAME", "PG_USER",
	            "PG_PASSWORD", "PG_SCHEMA", "REDIS_URL", "REDIS_HOST",
	            "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD"):
		monkeypatch.delenv(key, raising=False)
	yield monkeypatch
	os.environ.clear()
	os.environ.update(saved)


def _backend_writing_to(path) -> BackendServices:
	backend = object.__new__(BackendServices)   # no DB connection
	backend._CONFIG_ENV = path
	return backend


def test_runtime_env_wins_over_the_container_env(isolated_env, tmp_path):
	"""A host in runtime.env overrides the container env's; a key runtime.env
	doesn't set (the port) still comes from the container env."""
	isolated_env.setenv("PG_HOST", "postgres")          # container env
	isolated_env.setenv("PG_PORT", "5432")
	runtime = tmp_path / "runtime.env"
	runtime.write_text("PG_HOST=db.example.org\n")      # Server Management
	load_dotenv(runtime, override=True)                 # as BackendServices
	config = PostgresConfig.unload_env()
	assert (config.host, config.port) == ("db.example.org", "5432")


def test_container_env_is_used_without_a_runtime_env(isolated_env, tmp_path):
	"""With no runtime.env file, the Postgres host comes from the container env."""
	isolated_env.setenv("PG_HOST", "postgres")
	load_dotenv(tmp_path / "missing.env", override=True)
	assert PostgresConfig.unload_env().host == "postgres"


def test_a_switch_overrides_everything_inherited(isolated_env, tmp_path):
	"""Values from the container env that the new target doesn't use (a URL, a
	password, a schema) don't survive a switch written to runtime.env: the new
	host is used, with no schema and a Redis URL without a password."""
	isolated_env.setenv("DATABASE_URL", "postgresql://u:p@postgres/old")
	isolated_env.setenv("PG_SCHEMA", "old_schema")
	isolated_env.setenv("REDIS_URL", "redis://:pw@redis:6379/0")
	isolated_env.setenv("REDIS_PASSWORD", "compose-password")
	backend = _backend_writing_to(tmp_path / "config" / "runtime.env")
	backend._write_config(PostgresConfig(host="db.example.org").to_env_dict())
	backend._write_config(RedisConfig(host="cache.example.org").to_env_dict())
	load_dotenv(backend._CONFIG_ENV, override=True)
	pg, rd = PostgresConfig.unload_env(), RedisConfig.unload_env()
	assert make_url(pg.get_url()).host == "db.example.org"
	assert not pg.schema
	assert rd.get_url() == "redis://cache.example.org:6379/0"   # no password


def test_write_config_is_atomic_merged_and_owner_only(tmp_path):
	"""_write_config merges into runtime.env (a Postgres switch keeps the Redis
	key), leaves no temporary file behind, and the file is 600 on POSIX."""
	backend = _backend_writing_to(tmp_path / "config" / "runtime.env")
	backend._write_config({"PG_HOST": "a", "REDIS_HOST": "r"})
	backend._write_config({"PG_HOST": "b"})    # a Postgres switch keeps Redis
	assert dotenv_values(backend._CONFIG_ENV) == {"PG_HOST": "b",
	                                             "REDIS_HOST": "r"}
	assert list((tmp_path / "config").iterdir()) == [backend._CONFIG_ENV]
	if os.name == "posix":
		assert stat.S_IMODE(backend._CONFIG_ENV.stat().st_mode) == 0o600


def test_a_switch_no_longer_writes_external_flags():
	"""Neither the Postgres nor the Redis config writes a *_EXTERNAL key."""
	for config in (PostgresConfig(host="10.0.0.5"), RedisConfig(host="10.0.0.5")):
		assert not [k for k in config.to_env_dict() if k.endswith("_EXTERNAL")]


# ── Bundled or external ──────────────────────────────────────────────────────

def _backend_connected_to(pg_url: str, redis_host: str,
                          runtime_env: Path | None = None) -> BackendServices:
	"""A BackendServices with no real connection that looks connected to these."""
	backend = object.__new__(BackendServices)
	# no runtime.env unless given: nothing remembered by a database move
	backend._CONFIG_ENV = runtime_env or Path("does-not-exist") / "runtime.env"
	backend.postgres = SimpleNamespace(
		engine=SimpleNamespace(url=make_url(pg_url)),
		config=PostgresConfig(url=pg_url))
	backend.redis = SimpleNamespace(client=SimpleNamespace(
		connection_pool=SimpleNamespace(
			connection_kwargs={"host": redis_host})))
	return backend


@pytest.mark.parametrize("pg_host, redis_host, expected", [
	("localhost", "127.0.0.1", ("bundled", "bundled")),     # development
	("postgres", "redis", ("bundled", "bundled")),          # compose services
	("db.example.org", "10.0.0.5", ("external", "external")),
	("redis", "postgres", ("external", "external")),        # names swapped
])
def test_connection_modes(pg_host, redis_host, expected):
	"""Each service is bundled on localhost / its own compose service name and
	external elsewhere (another host, or the two service names swapped)."""
	backend = _backend_connected_to(
		f"postgresql+psycopg2://u:p@{pg_host}:5432/db", redis_host)
	modes = backend.connection_modes()
	assert (modes["POSTGRES"], modes["REDIS"]) == expected


def test_after_a_move_the_bundled_database_is_known_by_its_whole_address(tmp_path):
	"""Once runtime.env remembers the bundled database's URL, another database on
	the same host (development: 127.0.0.1) is external - Move back must be
	offered - and the remembered one itself is bundled."""
	runtime_env = tmp_path / "runtime.env"
	runtime_env.write_text("NETROLLOUT_BUNDLED_DATABASE_URL="
	                       "postgresql+psycopg2://nr:pw@127.0.0.1:5432/netrollout\n")
	moved = _backend_connected_to("postgresql+psycopg2://app:x@127.0.0.1:5432/ops_db",
	                              "127.0.0.1", runtime_env)
	assert moved.connection_modes()["POSTGRES"] == "external"
	home = _backend_connected_to("postgresql+psycopg2://nr:pw@127.0.0.1:5432/netrollout",
	                             "127.0.0.1", runtime_env)
	assert home.connection_modes()["POSTGRES"] == "bundled"
