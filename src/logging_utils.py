"""Rollout logs - one file per rollout in the logs folder (web app and CLI
alike), and for a web job also the live log the page streams (Redis: a
history list and a pub/sub channel) - plus the logs folder's clean-up and a
console that never fails on a character."""
import datetime
import html
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Callable, cast

from src import runtime

if TYPE_CHECKING:   # the web app passes its client in; the CLI (.exe) has none
	import redis
	from redis.client import PubSub


# Default for the "log_retention_days" System Setting (src/db/settings.py);
# the CLI, which has no database, uses it directly. Log files are kept longer
# than job records so the logs folder can be browsed for older
# troubleshooting — the settings rules keep it >= the job retention.
LOG_RETENTION_DAYS = 60
LOG_PRUNE_INTERVAL_HOURS = 24


def prune_logs(retention_days: int = LOG_RETENTION_DAYS,
			   logs_dir: str | os.PathLike[str] | None = None) -> int:
	"""Delete *.log files (web app and CLI alike) not modified for
	`retention_days`. Uses the last-modified time, so a running job's file -
	still being appended to - is never removed. Files that can't be removed
	(locked, permissions) are skipped.

	:param logs_dir: the folder; runtime.logs_dir() when None
	:returns: how many files were removed"""
	cutoff = time.time() - retention_days * 86400
	removed = 0
	try:
		entries = os.scandir(os.fspath(logs_dir or runtime.logs_dir()))
	except FileNotFoundError:
		return 0
	with entries:
		for entry in entries:
			if not (entry.name.endswith(".log") and entry.is_file()):
				continue
			try:
				if entry.stat().st_mtime < cutoff:
					os.remove(entry.path)
					removed += 1
			except OSError:
				continue
	return removed


def prune_once(retention_days: Callable[[], int] | None = None,
			   logs_dir: str | os.PathLike[str] | None = None) -> tuple[int, int]:
	"""One pruning pass.

	:param retention_days: returns the days to keep (the System Setting); if
	 it fails (settings unreachable, database down) the default is used
	 rather than skipping the pass
	:param logs_dir: the folder; runtime.logs_dir() when None
	:returns: (days used, files removed)"""
	try:
		days = int(retention_days()) if retention_days else LOG_RETENTION_DAYS
	except Exception:
		days = LOG_RETENTION_DAYS
	removed = prune_logs(days, logs_dir)
	if removed:
		print(f"[NetRollout] Removed {removed} log file(s) older than "
			  f"{days} days", flush=True)
	return days, removed


def start_log_pruning(retention_days: Callable[[], int] | None = None) -> None:
	"""Prune now, then every LOG_PRUNE_INTERVAL_HOURS from a daemon thread -
	a server that never restarts still cleans up. Called by the web app's
	entry point.

	:param retention_days: read on every run (the System Setting), so a
	 change applies at the next run; the default when it fails"""
	def loop() -> None:
		while True:
			prune_once(retention_days)
			time.sleep(LOG_PRUNE_INTERVAL_HOURS * 3600)
	threading.Thread(target=loop, name="log-pruner", daemon=True).start()


def utf8_console() -> None:
	"""Make console output UTF-8 and never fatal. Messages contain
	characters like '→' and '—'; on a non-UTF-8 stdout (redirected output,
	a Windows service: cp1252) print() raised UnicodeEncodeError and failed
	the whole request. Called once by each entry point."""
	for stream in (sys.stdout, sys.stderr):
		if hasattr(stream, "reconfigure"):
			stream.reconfigure(encoding="utf-8", errors="replace")


RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
REGULAR = "\033[1m"
END = "\033[0m"

WEBAPP_RED = '<div class="text-danger">'
WEBAPP_GREEN = '<div class="text-success">'
WEBAPP_YELLOW = '<div class="text-warning">'
WEBAPP_END = "</div>"

COLORS = {
	"RED": RED,
	"GREEN": GREEN,
	"YELLOW": YELLOW,
}

ANSI_TO_HTML = {"RED": WEBAPP_RED, "GREEN": WEBAPP_GREEN, "YELLOW": WEBAPP_YELLOW}

class RolloutLogger:
	"""Writes a rollout's messages to its log file; a web job's notable ones
	also go to its live log (what the page streams)."""

	def __init__(self, webapp: bool, verbose: bool,
				 prefix: str = "rollout", job_id: str | None = None,
				 redis_client: "redis.Redis | None" = None):
		""":param webapp: messages are for the page (HTML) rather than a console
		:param verbose: every message is shown, not only the notable ones
		:param prefix: the log file's name starts with it
		:param job_id: a web job's id - names the file and its live log
		:param redis_client: where a web job's live log is (None for the CLI)"""
		self._log_lock = threading.Lock()
		self._webapp = webapp
		self._verbose = verbose
		self._redis = redis_client

		ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
		logs_dir = runtime.logs_dir()
		os.makedirs(logs_dir, exist_ok=True)
		self._channel_key: str | None
		self._history_key: str | None
		if job_id:
			self.logfile = os.path.join(logs_dir,
										f"{prefix}_{ts}_{job_id}.log")
			self._channel_key = f"job:{job_id}:logs"
			self._history_key = f"job:{job_id}:history"

		else:
			self.logfile = os.path.join(logs_dir,
										f"{prefix}_{ts}.log")
			self._channel_key, self._history_key = None, None

	def _live_log(self) -> tuple["redis.Redis", str, str]:
		"""(client, history key, channel key) of this job's live log.
		:raises RuntimeError: this logger has none (the CLI, a logger without a
		 job id) - a caller's mistake"""
		if self._redis is None or self._history_key is None or self._channel_key is None:
			raise RuntimeError("this logger has no live log (no Redis client or job id)")
		return self._redis, self._history_key, self._channel_key

	def _log(self, message: str) -> None:
		"""Append a timestamped line to the log file (threads take turns)."""
		with self._log_lock:
			with open(self.logfile, "a", encoding="utf-8") as file:
				timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
				file.write(f"{timestamp}\t{message}\n")

	def _msg(self, message: str, color: str = "") -> str:
		"""The message dressed for where it's shown: an HTML <div> with the
		page's colour class (the text escaped), or ANSI colours for a console.

		:param color: "red" / "green" / "yellow"; "" for none"""
		if self._webapp:
			message = html.escape(message)
			opening = ANSI_TO_HTML.get(color.upper()) if color else None
			if opening:
				return opening + message + WEBAPP_END
			return message
		else:
			if color:
				ansi = COLORS.get(color.upper())
				if ansi:
					return ansi + message + END
			return message


	def notify(self, message: str, color: str = "", important: bool = False) -> \
			None:
		"""Log a message. Every message goes to the file; the notable ones -
		errors (red), important ones, or all in verbose mode - are also shown:
		a web job's in its live log, the CLI's on the console.

		:param color: "red" (an error) / "green" / "yellow"; "" for none
		:param important: shown even when not verbose"""
		if self._webapp:
			if (important or self._verbose or color == "red") and self._channel_key:
				client, history, channel = self._live_log()
				content = self._msg(message, color)
				client.rpush(history, content)
				client.publish(channel, content)
			self._log(message)
			return None
		else:
			if important or self._verbose or color == "red":
				print(self._msg(message, color))
			self._log(message)

	def get_history(self) -> list[str]:
		""":returns: the live log so far (what a page opened late catches up on)"""
		client, history, _ = self._live_log()
		# redis-py types a reply as maybe-awaitable; this client is sync
		lines = cast(list[bytes], client.lrange(history, 0, -1))
		return [m.decode() for m in lines]

	def subscribe(self) -> "PubSub":
		""":returns: a subscription to the live log's new messages"""
		client, _, channel = self._live_log()
		ps = client.pubsub()
		ps.subscribe(channel)
		return ps

	def redis_cleanup(self) -> None:
		"""The job is over: tell the live log's readers ("__done__") and
		remove its keys."""
		client, history, channel = self._live_log()
		client.publish(channel, "__done__")
		client.delete(history)
		client.delete(channel)
