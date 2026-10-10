"""RolloutLogger (src/rollout/log.py): message colouring, the log file, the
console and the live log, and log pruning."""
import html
import io
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from src import runtime
from src.rollout.log import (LOG_PRUNE_INTERVAL_HOURS, LOG_RETENTION_DAYS, Console, LiveLog,
                            LogPruner, RolloutLogger,
                            prune_once, prune_logs, redact, utf8_console)


# ---------------------------------------------------------------------------
# logging_utils.py
# ---------------------------------------------------------------------------

class TestMsg(unittest.TestCase):

	def test_no_color_terminal(self):
		"""On the console, a message without a colour is returned unchanged."""
		self.assertEqual(Console.dress("hello"), "hello")

	def test_red_terminal(self):
		"""On the console, red wraps the message in ANSI escape codes."""
		result = Console.dress("error", "red")
		self.assertIn("error", result)
		self.assertIn("\033[", result)

	def test_green_terminal(self):
		"""On the console, green wraps the message in ANSI escape codes."""
		result = Console.dress("ok", "green")
		self.assertIn("ok", result)
		self.assertIn("\033[", result)

	def test_webapp_red(self):
		"""In the web app, red wraps the message in the text-danger class."""
		result = LiveLog.dress("error", "red")
		self.assertIn("text-danger", result)
		self.assertIn("error", result)

	def test_webapp_green(self):
		"""In the web app, green wraps the message in the text-success class."""
		result = LiveLog.dress("ok", "green")
		self.assertIn("text-success", result)
		self.assertIn("ok", result)

	def test_webapp_no_color(self):
		"""In the web app, a message without a colour is returned unchanged."""
		self.assertEqual(LiveLog.dress("plain"), "plain")

	def test_unknown_color_returns_plain(self):
		"""An unknown colour name ("purple") leaves the message plain."""
		self.assertEqual(Console.dress("hello", "purple"), "hello")


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


# ── The live log: best-effort writes, numbered messages, a reader's follow ──

def test_a_live_log_outage_never_fails_notify_and_the_file_has_the_line(tmp_path):
	"""Redis raising on the live log (rpush / publish) doesn't fail notify, and
	the line is in the log file all the same."""
	client = MagicMock()
	client.rpush.side_effect = ConnectionError("redis down")
	logger = RolloutLogger(webapp=True, verbose=False, job_id="job-1",
	                       redis_client=client)
	logger.logfile = str(tmp_path / "rollout.log")
	logger.notify("device 10.0.0.1 failed", "red")
	assert "device 10.0.0.1 failed" in (tmp_path / "rollout.log").read_text(encoding="utf-8")


def test_a_live_message_is_published_with_its_history_number(tmp_path):
	"""A streamed line goes to the history and is published as
	"<the history's length>\\t<line>" - how a reader tells what it already has."""
	client = MagicMock()
	client.rpush.return_value = 7
	logger = RolloutLogger(webapp=True, verbose=False, job_id="job-1",
	                       redis_client=client)
	logger.logfile = str(tmp_path / "rollout.log")
	logger.notify("rollout started", important=True)
	client.rpush.assert_called_once_with("job:job-1:history", "rollout started")
	client.publish.assert_called_once_with("job:job-1:logs", "7\trollout started")


class FakePubSub:
	"""A subscription replaying given replies to get_message (None once spent)."""

	def __init__(self, replies):
		self.replies = list(replies)
		self.closed = False

	def subscribe(self, channel):
		pass

	def get_message(self, timeout=None):
		return self.replies.pop(0) if self.replies else None

	def close(self):
		self.closed = True


def _message(data):
	return {"type": "message", "data": data.encode()}


def _following(history, replies):
	"""A web logger whose Redis has `history` and whose subscription replies
	`replies` (after the subscription's confirmation)."""
	client = MagicMock()
	ps = FakePubSub([{"type": "subscribe"}, *replies])
	client.pubsub.return_value = ps
	client.lrange.return_value = [line.encode() for line in history]
	logger = RolloutLogger(webapp=True, verbose=False, job_id="job-1",
	                       redis_client=client)
	return logger, ps


def test_follow_skips_a_line_both_in_the_history_and_published():
	"""A line logged between the subscription and the history read is in both:
	follow sends it once, then the newer lines, and ends at the done message."""
	logger, ps = _following(["one", "two"],
	                        [_message("2\ttwo"), _message("3\tthree"), _message("__done__")])
	assert list(logger.live_log.follow(lambda: False)) == ["one", "two", "three"]
	assert ps.closed


def test_follow_subscribes_before_reading_the_history():
	"""The subscription is in effect (confirmed) before the history is read - else
	a line logged in between is in neither."""
	order = []
	logger, ps = _following(["one"], [_message("__done__")])
	client = logger.live_log._store
	confirm = ps.get_message

	def get_message(timeout=None):
		reply = confirm(timeout)
		if reply and reply["type"] == "subscribe":
			order.append("subscribed")
		return reply
	ps.get_message = get_message
	client.lrange.side_effect = lambda *_: order.append("history") or [b"one"]
	assert list(logger.live_log.follow(lambda: False)) == ["one"]
	assert order == ["subscribed", "history"]


def test_follow_sends_heartbeats_until_the_job_is_over():
	"""With nothing coming, follow yields None (a heartbeat) while the job isn't
	over, and ends once it is."""
	logger, _ = _following([], [])
	over = iter([False, False, True])
	assert list(logger.live_log.follow(lambda: next(over), wait=0.01)) == [None, None]


def test_follow_keeps_a_multi_line_message_whole():
	"""A published line with newlines in it comes out as one line."""
	logger, _ = _following([], [_message("1\tfirst\nsecond"), _message("__done__")])
	assert list(logger.live_log.follow(lambda: False)) == ["first\nsecond"]


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


def test_log_pruner_is_a_periodic_task_running_prune_once(tmp_path, monkeypatch):
	"""LogPruner is a runtime.PeriodicTask named "log-pruner": no first wait,
	then every LOG_PRUNE_INTERVAL_HOURS; a turn prunes the logs folder with
	the setting (10 days: a 20-day-old log removed, a 5-day-old one kept)."""
	monkeypatch.setenv(runtime.HOME_ENV, str(tmp_path))
	(tmp_path / "logs").mkdir()
	old = log_file(tmp_path / "logs", "rollout_a.log", 20)
	fresh = log_file(tmp_path / "logs", "rollout_b.log", 5)
	pruner = LogPruner(lambda: 10)
	assert isinstance(pruner, runtime.PeriodicTask)
	assert (pruner.name, pruner.interval, pruner.first_delay) == 		("log-pruner", LOG_PRUNE_INTERVAL_HOURS * 3600, None)
	pruner.run_once()
	assert not old.exists() and fresh.exists()


# ---------------------------------------------------------------------------
# redaction: no secret in a log (decision 16 - job logs are safe to share)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
	# Cisco IOS / IOS-XE / NX-OS / Arista
	("username ops privilege 15 password 0 S3cret!",
	 "username ops privilege 15 password 0 <redacted>"),
	("enable secret 9 $9$abcDEF$xyz", "enable secret 9 <redacted>"),
	("username ops secret sha512 $6$salt$hash", "username ops secret sha512 <redacted>"),
	("snmp-server community Pr1vate RO", "snmp-server community <redacted> RO"),
	("tacacs-server key 7 0822455D0A16", "tacacs-server key 7 <redacted>"),
	(" key-string 7 0507030E", " key-string 7 <redacted>"),
	("ip ospf message-digest-key 1 md5 OspfPass", "ip ospf message-digest-key 1 md5 <redacted>"),
	("snmp-server user mon grp v3 auth sha AuthPass1 priv aes 128 PrivPass2",
	 "snmp-server user mon grp v3 auth sha <redacted> priv aes 128 <redacted>"),
	# IOS-XR
	("username ops secret 10 $6$x$y", "username ops secret 10 <redacted>"),
	# Junos (quoted values, spaces kept inside the quotes)
	('set system login user ops authentication encrypted-password "$6$a$b"',
	 "set system login user ops authentication encrypted-password <redacted>"),
	('set protocols ospf area 0 interface ge-0/0/0 authentication md5 1 key "two words"',
	 "set protocols ospf area 0 interface ge-0/0/0 authentication md5 1 key <redacted>"),
	('set snmp community "Pr1v ate" authorization read-only',
	 "set snmp community <redacted> authorization read-only"),
	# PAN-OS
	("set network ike gateway gw1 authentication pre-shared-key key Psk123",
	 "set network ike gateway gw1 authentication pre-shared-key key <redacted>"),
	("set mgt-config users ops phash $1$salt$hash", "set mgt-config users ops phash <redacted>"),
	# FortiOS
	("set psksecret ENC abc123==", "set psksecret ENC <redacted>"),
	("set passwd Forti123", "set passwd <redacted>"),
	('set key "RadKey1"', "set key <redacted>"),
	# Comware
	("password cipher $c$3$abc", "password cipher <redacted>"),
	("snmp-agent community read simple Pr1v", "snmp-agent community read simple <redacted>"),
	# Aruba CX / ProCurve
	("user ops group administrators password plaintext Aruba1",
	 "user ops group administrators password plaintext <redacted>"),
	("password manager user-name admin plaintext Proc1",
	 "password <redacted> user-name admin plaintext <redacted>"),
	# Check Point Gaia
	("set user ops password-hash $6$a$b", "set user ops password-hash <redacted>"),
	# a device's complaint echoing the command, mid-line
	("R1(config)#username ops password 0 S3cret % Invalid input",
	 "R1(config)#username ops password 0 <redacted> % Invalid input"),
	# a log line quoting the command in single quotes
	("10.0.0.1:22: 'username ops password 0 S3cret' rejected - % Invalid input",
	 "10.0.0.1:22: 'username ops password 0 <redacted>' rejected - % Invalid input"),
])
def test_redact_hides_the_secret_and_keeps_the_rest(line, expected):
	"""Each platform family's secret-bearing line keeps its keyword, the
	encryption type and everything else; only the value becomes <redacted>."""
	assert redact(line) == expected


@pytest.mark.parametrize("line", [
	"crypto key generate rsa modulus 2048",
	"key chain BGP-KEYS",
	"password-policy min-length 8",
	"no password",
	"no enable secret",
	"description reset the password",
	"show running-config | include password",
	"connecting to 10.0.0.1:22",
	"Configuration rollout complete: 3 succeeded",
	"",
])
def test_redact_leaves_lines_without_a_secret_alone(line):
	"""Lines with no secret value - a keyword with nothing after it, key as an
	ordinary word, ordinary log lines - come back unchanged."""
	assert redact(line) == line


def test_no_secret_reaches_the_file_the_live_log_or_the_console(tmp_path, capsys):
	"""A rejected secret command logged through RolloutLogger.notify: the log
	file (also what Download Log serves), the live log's history and its
	published line, and the CLI console carry <redacted>, never the secret."""
	client = MagicMock()
	client.rpush.return_value = 1
	web = RolloutLogger(webapp=True, verbose=False, job_id="job-1", redis_client=client)
	web.logfile = str(tmp_path / "web.log")
	cli = RolloutLogger(webapp=False, verbose=True)
	cli.logfile = str(tmp_path / "cli.log")
	line = "10.0.0.1:22: 'username ops password 0 S3cret' rejected - % Invalid input"
	for logger in (web, cli):
		logger.notify(line, "red")

	for name in ("web.log", "cli.log"):
		text = (tmp_path / name).read_text(encoding="utf-8")
		assert "<redacted>" in text and "S3cret" not in text
	(_key, pushed), _ = client.rpush.call_args
	(_channel, published), _ = client.publish.call_args
	# the live log carries the line HTML-escaped (the page shows it as text)
	assert "S3cret" not in pushed + published and "<redacted>" in html.unescape(pushed)
	console = capsys.readouterr().out
	assert "<redacted>" in console and "S3cret" not in console
