"""Where NetRollout keeps its files (src/runtime.py) and which config wins
(BackendServices: config/runtime.env over the container environment)."""
import os
import sys

from src import runtime
from src.rollout.log import RolloutLogger, prune_logs


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
