"""Where NetRollout keeps its files (src/paths.py) and which config wins
(BackendServices: config/runtime.env over the container environment)."""
import os
import stat
import sys
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from src import paths
from src.db.backend import BackendServices
from src.db.postgres_db import PostgresConfig
from src.db.redis_db import RedisConfig
from src.logging_utils import RolloutLogger, prune_logs


# ── Folders ──────────────────────────────────────────────────────────────────

def test_netrollout_home_wins(monkeypatch, tmp_path):
	monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
	monkeypatch.setattr(sys, "frozen", True, raising=False)
	assert paths.home() == tmp_path
	assert paths.logs_dir() == tmp_path / "logs"
	assert paths.config_dir() == tmp_path / "config"
	assert paths.certs_dir() == tmp_path / "certs"
	assert paths.runtime_env() == tmp_path / "config" / "runtime.env"


def test_frozen_exe_uses_its_own_folder(monkeypatch, tmp_path):
	monkeypatch.delenv(paths.HOME_ENV)
	monkeypatch.setattr(sys, "frozen", True, raising=False)
	monkeypatch.setattr(sys, "executable", str(tmp_path / "netrollout-cli.exe"))
	assert paths.logs_dir() == tmp_path.resolve() / "logs"


def test_development_uses_the_repo_root(monkeypatch):
	monkeypatch.delenv(paths.HOME_ENV)
	assert not getattr(sys, "frozen", False)
	assert paths.home() == paths.REPO_ROOT
	assert (paths.REPO_ROOT / "src" / "paths.py").is_file()


def test_folders_follow_a_home_change_after_import(monkeypatch, tmp_path):
	# Resolved per call: the rollout logger and pruning follow NETROLLOUT_HOME
	monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
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
	from dotenv import load_dotenv
	isolated_env.setenv("PG_HOST", "postgres")          # container env
	isolated_env.setenv("PG_PORT", "5432")
	runtime = tmp_path / "runtime.env"
	runtime.write_text("PG_HOST=db.example.org\n")      # Server Management
	load_dotenv(runtime, override=True)                 # as BackendServices
	config = PostgresConfig.unload_env()
	assert (config.host, config.port) == ("db.example.org", "5432")


def test_container_env_is_used_without_a_runtime_env(isolated_env, tmp_path):
	from dotenv import load_dotenv
	isolated_env.setenv("PG_HOST", "postgres")
	load_dotenv(tmp_path / "missing.env", override=True)
	assert PostgresConfig.unload_env().host == "postgres"


def test_a_switch_blanks_an_inherited_url(isolated_env, tmp_path):
	# A DATABASE_URL / REDIS_URL from the container env would otherwise win
	# over the PG_* / REDIS_* the switch wrote, after the next restart
	from dotenv import load_dotenv
	isolated_env.setenv("DATABASE_URL", "postgresql://u:p@postgres/old")
	isolated_env.setenv("REDIS_URL", "redis://redis:6379/0")
	backend = _backend_writing_to(tmp_path / "config" / "runtime.env")
	backend._write_config(*PostgresConfig(host="db.example.org").to_env_dict())
	backend._write_config(*RedisConfig(host="cache.example.org").to_env_dict())
	load_dotenv(backend._CONFIG_ENV, override=True)
	assert make_url(PostgresConfig.unload_env().get_url()).host == \
	       "db.example.org"
	assert "cache.example.org" in RedisConfig.unload_env().get_url()


def test_write_config_is_atomic_merged_and_owner_only(tmp_path):
	backend = _backend_writing_to(tmp_path / "config" / "runtime.env")
	backend._write_config({"PG_HOST": "a", "PG_SCHEMA": "s"})
	backend._write_config({"PG_PORT": "5433"}, pop_keys=["PG_SCHEMA"])
	assert dotenv_values(backend._CONFIG_ENV) == {"PG_HOST": "a",
	                                             "PG_PORT": "5433"}
	assert list((tmp_path / "config").iterdir()) == [backend._CONFIG_ENV]
	if os.name == "posix":
		assert stat.S_IMODE(backend._CONFIG_ENV.stat().st_mode) == 0o600


def test_a_switch_no_longer_writes_external_flags():
	for config in (PostgresConfig(host="10.0.0.5"), RedisConfig(host="10.0.0.5")):
		updates, pop_keys = config.to_env_dict()
		assert not [k for k in [*updates, *pop_keys] if k.endswith("_EXTERNAL")]


# ── Bundled or external ──────────────────────────────────────────────────────

def _backend_connected_to(pg_url: str, redis_host: str) -> BackendServices:
	backend = object.__new__(BackendServices)
	backend.postgres = SimpleNamespace(
		engine=SimpleNamespace(url=make_url(pg_url)))
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
