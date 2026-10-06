"""Where NetRollout keeps its files (src/runtime.py) and which config wins
(BackendServices: config/runtime.env over the container environment)."""
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values, load_dotenv
from sqlalchemy.engine import make_url

from src import runtime
from src.db.backend import BackendServices
from src.db.postgres_db import PostgresConfig
from src.db.redis_db import RedisConfig
from src.logging_utils import RolloutLogger, prune_logs


# ── Folders ──────────────────────────────────────────────────────────────────

def test_netrollout_home_wins(monkeypatch, tmp_path):
	monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path))
	monkeypatch.setattr(sys, "frozen", True, raising=False)
	assert runtime.home() == tmp_path
	assert runtime.logs_dir() == tmp_path / "logs"
	assert runtime.config_dir() == tmp_path / "config"
	assert runtime.certs_dir() == tmp_path / "certs"
	assert runtime.runtime_env() == tmp_path / "config" / "runtime.env"


def test_frozen_exe_uses_its_own_folder(monkeypatch, tmp_path):
	monkeypatch.delenv(runtime.HOME_ENV)
	monkeypatch.setattr(sys, "frozen", True, raising=False)
	monkeypatch.setattr(sys, "executable", str(tmp_path / "netrollout-cli.exe"))
	assert runtime.logs_dir() == tmp_path.resolve() / "logs"


def test_development_uses_the_repo_root(monkeypatch):
	monkeypatch.delenv(runtime.HOME_ENV)
	assert not getattr(sys, "frozen", False)
	assert runtime.home() == runtime.REPO_ROOT
	assert (runtime.REPO_ROOT / "src" / "runtime.py").is_file()


def test_folders_follow_a_home_change_after_import(monkeypatch, tmp_path):
	# Resolved per call: the rollout logger and pruning follow NETROLLOUT_HOME
	monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path))
	logger = RolloutLogger(webapp=False, verbose=False, job_id="abc")
	assert os.path.dirname(logger.logfile) == str(tmp_path / "logs")
	old = tmp_path / "logs" / "old.log"
	old.write_text("x")
	os.utime(old, (0, 0))
	assert prune_logs(60) == 1


# ── Config precedence ────────────────────────────────────────────────────────

@pytest.fixture
def isolated_env(monkeypatch):
	# load_dotenv writes os.environ directly: restore all of it afterwards
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
	isolated_env.setenv("PG_HOST", "postgres")          # container env
	isolated_env.setenv("PG_PORT", "5432")
	runtime = tmp_path / "runtime.env"
	runtime.write_text("PG_HOST=db.example.org\n")      # Server Management
	load_dotenv(runtime, override=True)                 # as BackendServices
	config = PostgresConfig.unload_env()
	assert (config.host, config.port) == ("db.example.org", "5432")


def test_container_env_is_used_without_a_runtime_env(isolated_env, tmp_path):
	isolated_env.setenv("PG_HOST", "postgres")
	load_dotenv(tmp_path / "missing.env", override=True)
	assert PostgresConfig.unload_env().host == "postgres"


def test_a_switch_overrides_everything_inherited(isolated_env, tmp_path):
	# Values from the container env that the new target doesn't use (a URL,
	# a password, a schema) must not survive the switch and the next restart
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
	backend = _backend_writing_to(tmp_path / "config" / "runtime.env")
	backend._write_config({"PG_HOST": "a", "REDIS_HOST": "r"})
	backend._write_config({"PG_HOST": "b"})    # a Postgres switch keeps Redis
	assert dotenv_values(backend._CONFIG_ENV) == {"PG_HOST": "b",
	                                             "REDIS_HOST": "r"}
	assert list((tmp_path / "config").iterdir()) == [backend._CONFIG_ENV]
	if os.name == "posix":
		assert stat.S_IMODE(backend._CONFIG_ENV.stat().st_mode) == 0o600


def test_a_switch_no_longer_writes_external_flags():
	for config in (PostgresConfig(host="10.0.0.5"), RedisConfig(host="10.0.0.5")):
		assert not [k for k in config.to_env_dict() if k.endswith("_EXTERNAL")]


# ── Bundled or external ──────────────────────────────────────────────────────

def _backend_connected_to(pg_url: str, redis_host: str,
                          runtime_env: Path | None = None) -> BackendServices:
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
	backend = _backend_connected_to(
		f"postgresql+psycopg2://u:p@{pg_host}:5432/db", redis_host)
	modes = backend.connection_modes()
	assert (modes["POSTGRES"], modes["REDIS"]) == expected


def test_after_a_move_the_bundled_database_is_known_by_its_whole_address(tmp_path):
	# another database on the bundled one's host (development: 127.0.0.1) is
	# the organisation's - Move back must be offered
	runtime_env = tmp_path / "runtime.env"
	runtime_env.write_text("NETROLLOUT_BUNDLED_DATABASE_URL="
	                       "postgresql+psycopg2://nr:pw@127.0.0.1:5432/netrollout\n")
	moved = _backend_connected_to("postgresql+psycopg2://app:x@127.0.0.1:5432/ops_db",
	                              "127.0.0.1", runtime_env)
	assert moved.connection_modes()["POSTGRES"] == "external"
	home = _backend_connected_to("postgresql+psycopg2://nr:pw@127.0.0.1:5432/netrollout",
	                             "127.0.0.1", runtime_env)
	assert home.connection_modes()["POSTGRES"] == "bundled"
