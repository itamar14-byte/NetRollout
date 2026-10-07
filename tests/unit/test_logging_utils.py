"""logging_utils: console output that survives a non-UTF-8 stdout
(utf8_console), and the log-file retention (prune_logs, prune_once)."""
import io
import os
import sys
import time

import pytest

from src.logging_utils import (LOG_RETENTION_DAYS, RolloutLogger, prune_once,
                               prune_logs, utf8_console)


def cp1252_stream():
	"""What stdout looks like when output is redirected on Windows."""
	return io.TextIOWrapper(io.BytesIO(), encoding="cp1252")


def test_non_utf8_console_used_to_fail_notify(monkeypatch):
	"""Without utf8_console, an important notify with a '→' on a cp1252 stdout
	raises UnicodeEncodeError (the failure utf8_console fixes)."""
	monkeypatch.setattr(sys, "stdout", cp1252_stream())
	logger = RolloutLogger(webapp=False, verbose=False)
	with pytest.raises(UnicodeEncodeError):
		logger.notify("Bulk assign started: 2 devices → profile p",
		              important=True)


def test_utf8_console_makes_notify_safe(monkeypatch):
	"""After utf8_console, cp1252 stdout and stderr take '→' and '—': both are
	written as UTF-8."""
	out, err = cp1252_stream(), cp1252_stream()
	monkeypatch.setattr(sys, "stdout", out)
	monkeypatch.setattr(sys, "stderr", err)
	utf8_console()
	RolloutLogger(webapp=False, verbose=False).notify(
		"Bulk assign started: 2 devices → profile p", important=True)
	print("Startup aborted — key problem", file=sys.stderr)
	out.flush(), err.flush()
	assert "→".encode() in out.buffer.getvalue()
	assert "—".encode() in err.buffer.getvalue()


# ── Log retention ────────────────────────────────────────────────────────────

DAY = 86400


def log_file(folder, name, age_days, content="x"):
	"""A file in folder whose modification time is age_days old."""
	path = folder / name
	path.write_text(content)
	stamp = time.time() - age_days * DAY
	os.utime(path, (stamp, stamp))
	return path


def test_prune_removes_only_old_log_files(tmp_path):
	"""prune_logs(60) removes the two .log files older than 60 days; an old
	non-.log file, a fresh log, an old-named log modified today and a directory
	named .log are kept."""
	old_web = log_file(tmp_path, "rollout_20260101_000000_ab12.log", 61)
	old_cli = log_file(tmp_path, "cli_rollout_20260101_000000.log", 61)
	old_other = log_file(tmp_path, "notes.txt", 61)        # not a .log
	fresh = log_file(tmp_path, "bulk_var_assign_20260928_000000_cd34.log", 1)
	# named long ago but still being written (a running job): mtime decides
	running = log_file(tmp_path, "rollout_20250101_000000_ef56.log", 0)
	(tmp_path / "archive.log").mkdir()                     # a directory
	assert prune_logs(60, str(tmp_path)) == 2
	assert not old_web.exists() and not old_cli.exists()
	assert old_other.exists() and fresh.exists() and running.exists()
	assert (tmp_path / "archive.log").is_dir()


def test_prune_skips_files_it_cannot_remove(tmp_path, monkeypatch):
	"""A log file whose removal raises PermissionError is skipped: the other old
	log is still removed, and the count is 1."""
	locked = log_file(tmp_path, "rollout_a.log", 90)
	other = log_file(tmp_path, "rollout_b.log", 90)
	real_remove = os.remove

	def remove(path):
		if path.endswith("rollout_a.log"):
			raise PermissionError("in use")
		real_remove(path)
	monkeypatch.setattr(os, "remove", remove)
	assert prune_logs(60, str(tmp_path)) == 1
	assert locked.exists() and not other.exists()


def test_prune_missing_folder_is_a_no_op(tmp_path):
	"""prune_logs on a folder that doesn't exist removes nothing (0)."""
	assert prune_logs(60, str(tmp_path / "nope")) == 0


def test_prune_once_uses_the_setting_and_survives_its_failure(tmp_path):
	"""prune_once uses the retention setting (10 days: a 20-day-old log removed);
	when reading the setting raises, it prunes with LOG_RETENTION_DAYS instead."""
	log_file(tmp_path, "rollout_a.log", 20)
	# the setting (a callable, read on every pass) says 10 days
	assert prune_once(lambda: 10, str(tmp_path)) == (10, 1)
	log_file(tmp_path, "rollout_b.log", 20)

	def unreachable():
		raise ConnectionError("db down")
	# settings unreachable: prune with the default rather than skip the pass
	assert prune_once(unreachable, str(tmp_path)) == (LOG_RETENTION_DAYS, 0)
	# (log ≥ job retention is now a System Settings rule: tests/unit/test_settings)
