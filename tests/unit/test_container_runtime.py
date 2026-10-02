"""Stage 3 (container runtime) without services: the deployment mode, both
secret checks, the container startup line and how a deliberate stop exits."""
import os
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
import src.webapp.lifecycle as lifecycle
from src import runtime
from src.webapp.setup import resolve_secret_key
from src.webapp.startup import container_announcement, should_open_browser


@pytest.fixture
def container(monkeypatch):
	monkeypatch.setenv(runtime.DEPLOYMENT_ENV, "docker")


@pytest.fixture
def dev(monkeypatch):
	monkeypatch.delenv(runtime.DEPLOYMENT_ENV, raising=False)


# ── Deployment mode ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
	("docker", True), ("Docker ", True), ("", False), ("podman", False)])
def test_in_container_is_explicit(monkeypatch, value, expected):
	monkeypatch.setenv(runtime.DEPLOYMENT_ENV, value)
	assert runtime.in_container() is expected


def test_no_guessing_from_dockerenv(dev):
	# the old check looked for /.dockerenv; only the variable counts now
	assert runtime.in_container() is False
	assert should_open_browser({}, "win32") is True


@pytest.mark.parametrize("value, expected", [
	(None, runtime.DEFAULT_DRAIN_SECONDS), ("30", 30), ("-5", 0),
	("soon", runtime.DEFAULT_DRAIN_SECONDS)])
def test_drain_seconds(monkeypatch, value, expected):
	if value is None:
		monkeypatch.delenv(runtime.DRAIN_SECONDS_ENV, raising=False)
	else:
		monkeypatch.setenv(runtime.DRAIN_SECONDS_ENV, value)
	assert runtime.drain_seconds() == expected


# ── SECRET_KEY ───────────────────────────────────────────────────────────────

def test_secret_key_from_the_environment(container):
	assert resolve_secret_key({"SECRET_KEY": " s3cret "}) == "s3cret"


def test_missing_secret_key_refuses_in_a_container(container):
	with pytest.raises(runtime.StartupError, match="SECRET_KEY is not set"):
		resolve_secret_key({})


def test_missing_secret_key_is_random_per_run_in_dev(dev, capsys):
	first, second = resolve_secret_key({}), resolve_secret_key({})
	assert first != second and len(first) == 64 and first != "dev"
	assert "random key for this run" in capsys.readouterr().out


# ── Encryption key in a container ────────────────────────────────────────────

@pytest.fixture
def no_env_key(monkeypatch):
	monkeypatch.delenv(enc.ENV_VAR, raising=False)
	yield
	if enc.KEY_FILE.exists():
		enc.KEY_FILE.unlink()


def test_container_never_generates_a_key(container, no_env_key):
	with pytest.raises(enc.EncryptionStartupError, match="never generated"):
		enc.init_encryption(None)          # a fresh install, in dev: generates
	assert not enc.KEY_FILE.exists()


def test_container_ignores_a_key_file(container, no_env_key):
	# a file inside the container would vanish at the next update
	enc.KEY_DIR.mkdir(parents=True, exist_ok=True)
	enc.KEY_FILE.write_bytes(Fernet.generate_key())
	with pytest.raises(enc.EncryptionStartupError):
		enc.init_encryption(None)


def test_container_uses_the_env_key(container, monkeypatch):
	monkeypatch.setenv(enc.ENV_VAR, Fernet.generate_key().decode())
	enc.init_encryption(None)
	assert enc.decrypt(enc.encrypt("x")) == "x"


def test_container_key_is_checked_without_a_database(container, no_env_key):
	# launch_app calls this before BackendServices touches Postgres
	with pytest.raises(enc.EncryptionStartupError):
		enc.require_key_in_container()


def test_encryption_error_is_a_startup_error():
	# the entry point catches StartupError for a readable abort
	assert issubclass(enc.EncryptionStartupError, runtime.StartupError)


# ── Container startup line ───────────────────────────────────────────────────

def test_container_announcement():
	assert "expected at https://netops-srv01:8443" in \
	       container_announcement("https://netops-srv01:8443")
	assert "no hostname set" in container_announcement(None)


def test_no_browser_in_a_container(container):
	assert should_open_browser({}, "win32") is False


# ── A deliberate stop ────────────────────────────────────────────────────────

@pytest.fixture
def exits(monkeypatch):
	"""Records what Shutdown._run would do instead of doing it."""
	calls = SimpleNamespace(drained=[], relaunched=[], exited=[], slept=[])
	monkeypatch.setattr(lifecycle.time, "sleep", calls.slept.append)
	monkeypatch.setattr(lifecycle.os, "_exit", calls.exited.append)
	monkeypatch.setattr(lifecycle.subprocess, "Popen",
	                    lambda cmd, env: calls.relaunched.append(env))
	orchestrator = SimpleNamespace(
		drain=lambda deadline, report: calls.drained.append(deadline))
	calls.shutdown = lifecycle.Shutdown(orchestrator)
	return calls


def test_restart_in_dev_drains_relaunches_and_exits(dev, exits):
	exits.shutdown._run(30, restart=True)
	assert exits.drained == [30] and exits.exited == [0]
	assert exits.slept == [lifecycle._EXIT_DELAY]   # the response goes out
	assert exits.relaunched[0][lifecycle.RELAUNCH_ENV] == "1"


def test_restart_in_a_container_leaves_it_to_the_restart_policy(container,
                                                                exits):
	exits.shutdown._run(30, restart=True)
	assert exits.exited == [0] and exits.relaunched == []


def test_stop_never_relaunches_and_exits_without_delay(dev, exits):
	# docker stop only waits so long (stop_grace_period / StopTimeout)
	exits.shutdown._run(30, restart=False)
	assert exits.exited == [0] and exits.relaunched == []
	assert exits.slept == []


def test_exit_happens_even_if_the_drain_fails(dev, exits):
	def broken(deadline, report):
		raise RuntimeError("redis gone")
	exits.shutdown._orchestrator = SimpleNamespace(drain=broken)
	with pytest.raises(RuntimeError):
		exits.shutdown._run(30, restart=False)
	assert exits.exited == [0]


def test_a_second_stop_request_is_ignored(monkeypatch):
	started = []
	monkeypatch.setattr(lifecycle.threading, "Thread",
	                    lambda **kw: SimpleNamespace(
		                    start=lambda: started.append(kw["args"])))
	shutdown = lifecycle.Shutdown(orchestrator=None)
	assert shutdown.begin(600, restart=False) is True
	assert shutdown.begin(0, restart=True) is False
	assert started == [(600, False)]
	assert shutdown.in_progress and not shutdown.restarting
