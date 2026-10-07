"""RolloutLogger (src/rollout/log.py): message colouring, the log file, the
console and the live log, and log pruning."""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.rollout.log import LOG_RETENTION_DAYS, RolloutLogger, prune_once, prune_logs, utf8_console


# ---------------------------------------------------------------------------
# logging_utils.py
# ---------------------------------------------------------------------------

class TestMsg(unittest.TestCase):

	def test_no_color_terminal(self):
		"""On the console, a message without a colour is returned unchanged."""
		logger = RolloutLogger(webapp=False, verbose=False)
		self.assertEqual(logger._msg("hello"), "hello")

	def test_red_terminal(self):
		"""On the console, red wraps the message in ANSI escape codes."""
		logger = RolloutLogger(webapp=False, verbose=False)
		result = logger._msg("error", "red")
		self.assertIn("error", result)
		self.assertIn("\033[", result)

	def test_green_terminal(self):
		"""On the console, green wraps the message in ANSI escape codes."""
		logger = RolloutLogger(webapp=False, verbose=False)
		result = logger._msg("ok", "green")
		self.assertIn("ok", result)
		self.assertIn("\033[", result)

	def test_webapp_red(self):
		"""In the web app, red wraps the message in the text-danger class."""
		logger = RolloutLogger(webapp=True, verbose=False)
		result = logger._msg("error", "red")
		self.assertIn("text-danger", result)
		self.assertIn("error", result)

	def test_webapp_green(self):
		"""In the web app, green wraps the message in the text-success class."""
		logger = RolloutLogger(webapp=True, verbose=False)
		result = logger._msg("ok", "green")
		self.assertIn("text-success", result)
		self.assertIn("ok", result)

	def test_webapp_no_color(self):
		"""In the web app, a message without a colour is returned unchanged."""
		logger = RolloutLogger(webapp=True, verbose=False)
		self.assertEqual(logger._msg("plain"), "plain")

	def test_unknown_color_returns_plain(self):
		"""An unknown colour name ("purple") leaves the message plain."""
		logger = RolloutLogger(webapp=False, verbose=False)
		self.assertEqual(logger._msg("hello", "purple"), "hello")


class TestLog(unittest.TestCase):

	def test_writes_message_to_file(self):
		"""_log writes the message into the logger's log file."""
		with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
			path = f.name
		try:
			logger = RolloutLogger(webapp=False, verbose=False)
			logger.logfile = path
			logger._log("test message")
			with open(path) as f:
				content = f.read()
			self.assertIn("test message", content)
		finally:
			os.unlink(path)

	def test_includes_timestamp(self):
		"""Each _log line carries a YYYY-MM-DD HH:MM:SS timestamp."""
		with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
			path = f.name
		try:
			logger = RolloutLogger(webapp=False, verbose=False)
			logger.logfile = path
			logger._log("timestamped")
			with open(path) as f:
				content = f.read()
			self.assertRegex(content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
		finally:
			os.unlink(path)

	def test_appends_multiple_entries(self):
		"""Two _log calls append two lines to the file."""
		with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
			path = f.name
		try:
			logger = RolloutLogger(webapp=False, verbose=False)
			logger.logfile = path
			logger._log("first")
			logger._log("second")
			with open(path) as f:
				lines = f.readlines()
			self.assertEqual(len(lines), 2)
		finally:
			os.unlink(path)


class TestBaseNotify(unittest.TestCase):

	def setUp(self):
		f = tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False)
		self.logfile = f.name
		f.close()

	def tearDown(self):
		os.unlink(self.logfile)

	def test_verbose_terminal_prints(self):
		"""On the console in verbose mode, notify prints the message once."""
		logger = RolloutLogger(webapp=False, verbose=True)
		logger.logfile = self.logfile
		with patch("builtins.print") as mock_print:
			logger.notify("hello", "green")
			mock_print.assert_called_once()

	def test_non_verbose_terminal_does_not_print(self):
		"""On the console without verbose, a plain green message isn't printed."""
		logger = RolloutLogger(webapp=False, verbose=False)
		logger.logfile = self.logfile
		with patch("builtins.print") as mock_print:
			logger.notify("hello", "green")
			mock_print.assert_not_called()

	def _webapp_logger(self, verbose):
		"""A web app logger for job-1 that streams to a mock Redis (history list +
		pub/sub channel); returns (logger, redis client)."""
		redis_client = MagicMock()
		logger = RolloutLogger(webapp=True, verbose=verbose, job_id="job-1",
							   redis_client=redis_client)
		logger.logfile = self.logfile
		return logger, redis_client

	def test_verbose_webapp_publishes(self):
		"""A verbose web app logger pushes the message to the job's history and
		publishes it once on job:job-1:logs."""
		logger, redis_client = self._webapp_logger(verbose=True)
		logger.notify("hello", "green")
		redis_client.publish.assert_called_once()
		redis_client.rpush.assert_called_once()
		self.assertEqual(redis_client.publish.call_args[0][0], "job:job-1:logs")

	def test_non_verbose_webapp_does_not_publish(self):
		"""A non-verbose web app logger keeps a plain green message out of Redis."""
		logger, redis_client = self._webapp_logger(verbose=False)
		logger.notify("hello", "green")
		redis_client.publish.assert_not_called()
		redis_client.rpush.assert_not_called()

	def test_non_verbose_webapp_publishes_errors_and_important(self):
		"""Without verbose, a red message and an important one are still published."""
		logger, redis_client = self._webapp_logger(verbose=False)
		logger.notify("boom", "red")
		logger.notify("milestone", important=True)
		self.assertEqual(redis_client.publish.call_count, 2)

	def test_webapp_without_job_id_never_touches_redis(self):
		"""A web app logger without a job id publishes nothing, even when important."""
		redis_client = MagicMock()
		logger = RolloutLogger(webapp=True, verbose=True,
							   redis_client=redis_client)
		logger.logfile = self.logfile
		logger.notify("hello", important=True)
		redis_client.publish.assert_not_called()

	def test_always_logs_to_file(self):
		"""notify writes the message to the log file even when it isn't shown."""
		logger = RolloutLogger(webapp=False, verbose=False)
		logger.logfile = self.logfile
		logger.notify("logged")
		with open(self.logfile) as f:
			content = f.read()
		self.assertIn("logged", content)


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
