"""Stage 3 (container runtime) without services: the deployment mode, both
secret checks, the container startup line and how a deliberate stop exits."""
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
import src.webapp.lifecycle as lifecycle
from src import runtime
from src.webapp.build import resolve_secret_key
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
	"""Only NETROLLOUT_DEPLOYMENT=docker (any case, spaces trimmed) means a
	container; empty or another value ("podman") does not."""
	monkeypatch.setenv(runtime.DEPLOYMENT_ENV, value)
	assert runtime.in_container() is expected


def test_no_guessing_from_dockerenv(dev):
	"""Without the variable it is not a container, whatever the host has (the old
	check looked for /.dockerenv; only the variable counts now), so the browser
	opens on a desktop launch."""
	assert runtime.in_container() is False
	assert should_open_browser({}, "win32") is True


@pytest.mark.parametrize("value, expected", [
	(None, runtime.DEFAULT_DRAIN_SECONDS), ("30", 30), ("-5", 0),
	("soon", runtime.DEFAULT_DRAIN_SECONDS)])
def test_drain_seconds(monkeypatch, value, expected):
	"""NETROLLOUT_DRAIN_SECONDS: unset or not a number gives the default, a
	number is used, a negative one becomes 0."""
	if value is None:
		monkeypatch.delenv(runtime.DRAIN_SECONDS_ENV, raising=False)
	else:
		monkeypatch.setenv(runtime.DRAIN_SECONDS_ENV, value)
	assert runtime.drain_seconds() == expected


# ── SECRET_KEY ───────────────────────────────────────────────────────────────

def test_secret_key_from_the_environment(container):
	"""In a container SECRET_KEY is taken from the environment, spaces trimmed."""
	assert resolve_secret_key({"SECRET_KEY": " s3cret "}) == "s3cret"


def test_missing_secret_key_refuses_in_a_container(container):
	"""In a container a missing SECRET_KEY is a StartupError ("SECRET_KEY is not
	set")."""
	with pytest.raises(runtime.StartupError, match="SECRET_KEY is not set"):
		resolve_secret_key({})


def test_missing_secret_key_is_random_per_run_in_dev(dev, capsys):
	"""In dev a missing SECRET_KEY gets a random 64-character key, different on
	each call and never "dev", and the console says so."""
	first, second = resolve_secret_key({}), resolve_secret_key({})
	assert first != second and len(first) == 64 and first != "dev"
	assert "random key for this run" in capsys.readouterr().out


# ── Encryption key in a container ────────────────────────────────────────────

@pytest.fixture
def no_env_key(monkeypatch):
	"""No encryption key in the environment; removes the dev key file afterwards."""
	monkeypatch.delenv(enc.ENV_VAR, raising=False)
	yield
	if enc.KEY_FILE.exists():
		enc.KEY_FILE.unlink()


def test_container_never_generates_a_key(container, no_env_key):
	"""In a container without a key, init_encryption refuses ("never generated")
	where dev would generate one, and writes no key file."""
	with pytest.raises(enc.EncryptionStartupError, match="never generated"):
		enc.init_encryption(None)          # a fresh install, in dev: generates
	assert not enc.KEY_FILE.exists()


def test_container_ignores_a_key_file(container, no_env_key):
	"""In a container a key file is not used: without the env key the start is
	refused even when the file exists (a file inside the container would vanish
	at the next update)."""
	enc.KEY_DIR.mkdir(parents=True, exist_ok=True)
	enc.KEY_FILE.write_bytes(Fernet.generate_key())
	with pytest.raises(enc.EncryptionStartupError):
		enc.init_encryption(None)


def test_container_uses_the_env_key(container, monkeypatch):
	"""In a container the key from the environment is used: encrypt then decrypt
	gives the text back."""
	monkeypatch.setenv(enc.ENV_VAR, Fernet.generate_key().decode())
	enc.init_encryption(None)
	assert enc.decrypt(enc.encrypt("x")) == "x"


def test_container_key_is_checked_without_a_database(container, no_env_key):
	"""require_key_in_container refuses a missing key without any database (launch_app
	calls it before BackendServices touches Postgres)."""
	with pytest.raises(enc.EncryptionStartupError):
		enc.require_key_in_container()


def test_encryption_error_is_a_startup_error():
	"""EncryptionStartupError is a StartupError, so the entry point's catch gives a
	readable abort."""
	assert issubclass(enc.EncryptionStartupError, runtime.StartupError)


# ── Container startup line ───────────────────────────────────────────────────

def test_container_announcement():
	"""The container's startup line names the expected URL, or says no hostname is
	set."""
	assert "expected at https://netops-srv01:8443" in \
	       container_announcement("https://netops-srv01:8443")
	assert "no hostname set" in container_announcement(None)


def test_no_browser_in_a_container(container):
	"""In a container no browser is opened, even on a desktop platform."""
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
	"""A dev restart drains with the given deadline, waits the exit delay, relaunches
	with the relaunch marker set and exits 0."""
	exits.shutdown._run(30, restart=True)
	assert exits.drained == [30] and exits.exited == [0]
	assert exits.slept == [lifecycle._EXIT_DELAY]   # the response goes out
	assert exits.relaunched[0][lifecycle.RELAUNCH_ENV] == "1"


def test_restart_in_a_container_leaves_it_to_the_restart_policy(container,
                                                                exits):
	"""A restart in a container exits 0 without relaunching: the restart policy
	brings it back."""
	exits.shutdown._run(30, restart=True)
	assert exits.exited == [0] and exits.relaunched == []


def test_stop_never_relaunches_and_exits_without_delay(dev, exits):
	"""A stop exits 0 at once, with no relaunch and no sleep (docker stop only waits
	so long: stop_grace_period / StopTimeout)."""
	exits.shutdown._run(30, restart=False)
	assert exits.exited == [0] and exits.relaunched == []
	assert exits.slept == []


def test_exit_happens_even_if_the_drain_fails(dev, exits):
	"""When the drain raises, the error propagates but the process still exits 0."""
	def broken(deadline, report):
		raise RuntimeError("redis gone")
	exits.shutdown._orchestrator = SimpleNamespace(drain=broken)
	with pytest.raises(RuntimeError):
		exits.shutdown._run(30, restart=False)
	assert exits.exited == [0]


def test_a_second_stop_request_is_ignored(monkeypatch):
	"""The first begin() starts the shutdown thread and returns True; a second one
	(a restart) returns False and changes nothing: one thread, still a stop."""
	started = []
	monkeypatch.setattr(lifecycle.threading, "Thread",
	                    lambda **kw: SimpleNamespace(
		                    start=lambda: started.append(kw["args"])))
	shutdown = lifecycle.Shutdown(orchestrator=None)
	assert shutdown.begin(600, restart=False) is True
	assert shutdown.begin(0, restart=True) is False
	assert started == [(600, False)]
	assert shutdown.in_progress and not shutdown.restarting


# ── Stage 6.1: version + source link ──

def test_the_version_is_the_version_file():
	"""runtime.VERSION equals the repo's VERSION file, stripped."""
	text = (runtime.REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
	assert runtime.VERSION == text


@pytest.mark.parametrize("content, expected", [
	("1.2.3\n", "1.2.3"), ("v1.2.3\r\n", "1.2.3"), ("  \n", "0.0.0.dev0"),
	(None, "0.0.0.dev0")])
def test_reading_the_version_file(tmp_path, monkeypatch, content, expected):
	"""The VERSION file is read from sys._MEIPASS (where the CLI .exe unpacks its
	bundled files): line ends and a leading "v" dropped; blank or missing gives
	0.0.0.dev0."""
	if content is not None:
		(tmp_path / "VERSION").write_text(content, encoding="utf-8", newline="")
	monkeypatch.setattr(runtime.sys, "_MEIPASS", str(tmp_path), raising=False)
	assert runtime._read_version() == expected


@pytest.mark.parametrize("version,url", [
	("1.0.0.dev0", "https://github.com/itamar14-byte/NetRollout"),
	("1.0.0", "https://github.com/itamar14-byte/NetRollout/tree/v1.0.0"),
	("1.1.0-rc1", "https://github.com/itamar14-byte/NetRollout/tree/v1.1.0-rc1"),
])
def test_source_url_points_at_the_running_version(monkeypatch, version, url):
	"""The source link is the repository for a dev version, else the version's tag
	(tree/v<version>)."""
	monkeypatch.setattr(runtime, "VERSION", version)
	assert runtime.source_url() == url


@pytest.mark.parametrize("value, expected", [
	(None, 32), ("16", 16), ("1", 4), ("100000", 256), ("lots", 32)])
def test_server_threads(monkeypatch, value, expected):
	"""NETROLLOUT_THREADS: unset or not a number gives 32, a number is kept within
	4-256 (Waitress's own default of 4 stalled the app with four open live logs)."""
	if value is None:
		monkeypatch.delenv(runtime.THREADS_ENV, raising=False)
	else:
		monkeypatch.setenv(runtime.THREADS_ENV, value)
	assert runtime.server_threads() == expected


def test_the_server_is_started_with_those_threads():
	"""The web app's __main__ passes threads=server_threads() to the server."""
	main = (Path(runtime.REPO_ROOT) / "src" / "webapp" / "__main__.py").read_text(encoding="utf-8")
	assert "threads=server_threads()" in main


def test_the_running_version_follows_the_file(tmp_path, monkeypatch):
	"""Reloading runtime with another VERSION file (9.9.9) gives that version, so it
	is read from the file, not a copy that happens to match; undone after."""
	(tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
	monkeypatch.setattr(runtime.sys, "_MEIPASS", str(tmp_path), raising=False)
	try:
		assert importlib.reload(runtime).VERSION == "9.9.9"
	finally:
		monkeypatch.undo()
		importlib.reload(runtime)
	assert runtime.VERSION != "9.9.9"
