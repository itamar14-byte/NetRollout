"""src/runtime.py: the folders (NETROLLOUT_HOME, the .exe's, the repo's), the
deployment mode, the drain time, the version and its source link, the
server's threads."""
import importlib
import os
import sys
from pathlib import Path

import pytest

from src import runtime
from src.rollout.log import RolloutLogger, prune_logs
from src.webapp.startup import should_open_browser


# ── Folders ──────────────────────────────────────────────────────────────────

def test_netrollout_home_wins(monkeypatch, tmp_path):
	"""NETROLLOUT_HOME wins even in a frozen exe: logs/, config/, certs/ and
	config/runtime.env all sit under it."""
	monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path))
	monkeypatch.setattr(sys, "frozen", True, raising=False)
	assert runtime.home() == tmp_path
	assert runtime.logs_dir() == tmp_path / "logs"
	assert runtime.config_dir() == tmp_path / "config"
	assert runtime.certs_dir() == tmp_path / "certs"
	assert runtime.runtime_env() == tmp_path / "config" / "runtime.env"


def test_frozen_exe_uses_its_own_folder(monkeypatch, tmp_path):
	"""Without NETROLLOUT_HOME, a frozen exe keeps its logs/ next to the exe."""
	monkeypatch.delenv(runtime.HOME_ENV)
	monkeypatch.setattr(sys, "frozen", True, raising=False)
	monkeypatch.setattr(sys, "executable", str(tmp_path / "netrollout-cli.exe"))
	assert runtime.logs_dir() == tmp_path.resolve() / "logs"


def test_development_uses_the_repo_root(monkeypatch):
	"""Without NETROLLOUT_HOME and not frozen, the home is the repo root (the
	folder holding src/runtime.py)."""
	monkeypatch.delenv(runtime.HOME_ENV)
	assert not getattr(sys, "frozen", False)
	assert runtime.home() == runtime.REPO_ROOT
	assert (runtime.REPO_ROOT / "src" / "runtime.py").is_file()


def test_folders_follow_a_home_change_after_import(monkeypatch, tmp_path):
	"""Folders are resolved per call: a NETROLLOUT_HOME set after import moves the
	rollout logger's file and log pruning (an old log there is pruned) with it."""
	monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path))
	logger = RolloutLogger(webapp=False, verbose=False, job_id="abc")
	assert os.path.dirname(logger.logfile) == str(tmp_path / "logs")
	old = tmp_path / "logs" / "old.log"
	old.write_text("x")
	os.utime(old, (0, 0))
	assert prune_logs(60) == 1


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
	# the module as it was: a reload makes every class and function anew, and
	# modules that imported them (build.py's StartupError) must keep theirs
	saved = dict(vars(runtime))
	try:
		assert importlib.reload(runtime).VERSION == "9.9.9"
	finally:
		monkeypatch.undo()
		vars(runtime).clear()
		vars(runtime).update(saved)
	assert runtime.VERSION != "9.9.9"

@pytest.mark.parametrize("version, name", [
	("1.0.0rc1", "Anycast"), ("1.0.0", "Anycast"), ("1.4.2", "Anycast"),   # every 1.x
	("0.0.0.dev0", None),       # development, no release
	("2.0.0", None),            # a major not named yet
	("garbage", None),
])
def test_a_release_is_named_after_its_major(version, name):
	"""The codename belongs to the major version: every 1.x is the first name
	(A); a version without a named major has none."""
	assert runtime.codename(version) == name
