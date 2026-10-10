"""Rollout logs - one file per rollout in the logs folder (web app and CLI
alike), and where its notable messages are shown (an Echo): the console
(Console) or, for a web job, the live log the page streams (LiveLog - Redis:
a history list and a pub/sub channel) - plus the logs folder's clean-up and
a console that never fails on a character.

Redis isn't imported here (the CLI .exe has none): the web app passes its
client in, typed by what is used of it (KeyValueStore)."""
import datetime
import html
import os
import sys
import threading
import time
from collections.abc import Iterator
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable, Protocol, cast

from src import runtime
if TYPE_CHECKING:   # annotations only: the CLI (.exe) has no Redis
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


class LogPruner(runtime.PeriodicTask):
	"""The logs folder's clean-up loop: prune_once() now, then every
	LOG_PRUNE_INTERVAL_HOURS - a server that never restarts still cleans up.
	Started by the web app's entry point (the CLI prunes once, prune_logs)."""
	FAILURE = f"The log clean-up failed: {{error}} (tried again in {LOG_PRUNE_INTERVAL_HOURS} hours)"

	def __init__(self, retention_days: Callable[[], int] | None = None) -> None:
		""":param retention_days: read on every run (the System Setting), so a
		 change applies at the next run; the default when it fails"""
		super().__init__("log-pruner", LOG_PRUNE_INTERVAL_HOURS * 3600)
		self.retention_days = retention_days

	def run_once(self) -> None:
		prune_once(self.retention_days)


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


class Tone(StrEnum):
	"""How a log message is shown: its value is the colour name the logger
	maps to ANSI (a console) or the page's class (COLORS / ANSI_TO_HTML).
	An ERROR is always shown, verbose or not."""
	ERROR = "red"
	WARNING = "yellow"
	SUCCESS = "green"
	INFO = ""

# The live log's end, published on its channel (unnumbered)
DONE = "__done__"
# How long subscribe() waits for Redis to confirm the subscription
_SUBSCRIBE_WAIT = 2.0


class KeyValueStore(Protocol):
	"""The Redis client as NetRollout uses it - LiveLog here, JobStore
	(src/jobs.py) - and no more: a redis.Redis is one (structurally; this
	module doesn't import redis). redis-py types each reply as
	maybe-awaitable (one signature for its sync and async clients), so the
	replies are Any here and each caller casts it to what it is."""

	def rpush(self, name: str, /, *values: str) -> Any: ...
	def lrange(self, name: str, start: int, end: int, /) -> Any: ...
	def lrem(self, name: str, count: int, value: str, /) -> Any: ...
	def blpop(self, keys: str, /, timeout: int) -> Any: ...
	def publish(self, channel: str, message: str, /) -> Any: ...
	def pubsub(self) -> "PubSub": ...
	def delete(self, *names: str) -> Any: ...
	def exists(self, *names: str) -> Any: ...
	def scan_iter(self, match: str, /) -> Iterator[Any]: ...
	def hset(self, name: str, /, *, mapping: dict[str, Any]) -> Any: ...
	def hgetall(self, name: str, /) -> Any: ...
	def sadd(self, name: str, /, *values: str) -> Any: ...
	def srem(self, name: str, /, *values: str) -> Any: ...
	def smembers(self, name: str, /) -> Any: ...
	def incr(self, name: str, /) -> Any: ...
	def decr(self, name: str, /) -> Any: ...
	def get(self, name: str, /) -> Any: ...
	def set(self, name: str, value: str, /, *, ex: int) -> Any: ...
	def eval(self, script: str, numkeys: int, /, *keys_and_args: str) -> Any: ...


class Echo(Protocol):
	"""Where a logger's notable messages are shown, each dressed for it."""

	def show(self, message: str, tone: str) -> None:
		""":param tone: a Tone (its colour name); Tone.INFO for none"""
		...


def live_log_keys(job_id: str) -> tuple[str, str]:
	"""The one spelling of a job's live log keys (JobStore clears leftovers
	with "*").

	:returns: (its history list, its pub/sub channel)"""
	return f"job:{job_id}:history", f"job:{job_id}:logs"


def _numbered(data: str) -> tuple[int | None, str]:
	"""A live message as published: "<n>\\t<line>", n the history's length
	once the line was added.

	:returns: (n - None when the message carries none, the line)"""
	head, tab, line = data.partition("\t")
	if tab and head.isdigit():
		return int(head), line
	return None, data


class Console:
	"""The terminal: ANSI colours, printed."""

	@staticmethod
	def dress(message: str, tone: str = Tone.INFO) -> str:
		""":returns: the message in its tone's ANSI colour (unchanged for none
		 or an unknown one)"""
		if tone:
			ansi = COLORS.get(tone.upper())
			if ansi:
				return ansi + message + END
		return message

	def show(self, message: str, tone: str) -> None:
		print(self.dress(message, tone))


class LiveLog:
	"""A web job's live log, what its page streams: a history list (a page
	opened late catches up on it) and a pub/sub channel (each new line) in
	Redis. Writing to it is best-effort - a Redis outage must not fail the
	rollout (and lose its results) over a log line."""

	def __init__(self, store: KeyValueStore, job_id: str) -> None:
		""":param store: where it is (the web app's Redis)
		:param job_id: whose (names its keys: live_log_keys)"""
		self._store = store
		self._history, self._channel = live_log_keys(job_id)

	@staticmethod
	def dress(message: str, tone: str = Tone.INFO) -> str:
		""":returns: the message for the page: HTML-escaped, in a <div> with
		 its tone's class (bare for none or an unknown one)"""
		message = html.escape(message)
		opening = ANSI_TO_HTML.get(tone.upper()) if tone else None
		if opening:
			return opening + message + WEBAPP_END
		return message

	def show(self, message: str, tone: str) -> None:
		self.append(self.dress(message, tone))

	def append(self, line: str) -> None:
		"""A line to the history, then published numbered - a reader that has
		read the history skips what it holds. Never raises."""
		try:
			length = cast(int, self._store.rpush(self._history, line))
			self._store.publish(self._channel, f"{length}\t{line}")
		except Exception:   # noqa: BLE001 - redis isn't imported here (the CLI .exe)
			pass

	def history(self) -> list[str]:
		""":returns: the live log so far (what a page opened late catches up on)"""
		lines = cast(list[bytes], self._store.lrange(self._history, 0, -1))
		return [m.decode() for m in lines]

	def subscribe(self) -> "PubSub":
		"""A subscription to the live log's new messages, in effect once this
		returns: Redis has confirmed it (or _SUBSCRIBE_WAIT passed), so a
		history read after it misses nothing.

		:returns: the subscription; its messages are "<n>\\t<line>" or DONE"""
		ps = self._store.pubsub()
		ps.subscribe(self._channel)
		deadline = time.monotonic() + _SUBSCRIBE_WAIT
		while (left := deadline - time.monotonic()) > 0:
			# redis-py: a dict per message (its stubs say otherwise)
			msg = cast(dict[str, Any] | None, ps.get_message(timeout=left))
			if msg and msg.get("type") == "subscribe":
				break
		return ps

	def follow(self, over: Callable[[], bool], wait: float = 0.5) -> Iterator[str | None]:
		"""The live log for a reader: the lines so far, then each new one -
		none lost or repeated between the two (subscribed first, then the
		history read; a live message the history already held is skipped by
		its number).

		:param over: whether the job has ended - asked when nothing came for
		 `wait` seconds (its end is normally the DONE message)
		:returns: the lines; None after `wait` seconds without one (a heartbeat)"""
		ps = self.subscribe()
		try:
			history = self.history()
			yield from history
			seen = len(history)
			while True:
				msg = cast(dict[str, Any] | None, ps.get_message(timeout=wait))
				if msg and msg["type"] == "message":
					data = cast(bytes, msg["data"]).decode()
					if data == DONE:
						return
					number, line = _numbered(data)
					if number is not None:
						if number <= seen:
							continue          # already sent with the history
						seen = number
					yield line
				elif over():
					return
				else:
					yield None
		finally:
			ps.close()

	def close(self) -> None:
		"""The job is over: tell the readers (DONE) and remove the keys."""
		self._store.publish(self._channel, DONE)
		self._store.delete(self._history)
		self._store.delete(self._channel)


class RolloutLogger:
	"""Writes a rollout's messages to its log file; the notable ones are also
	shown - the CLI's on the console, a web job's in its live log."""

	def __init__(self, webapp: bool, verbose: bool,
				 prefix: str = "rollout", job_id: str | None = None,
				 redis_client: KeyValueStore | None = None):
		""":param webapp: messages are for the page (HTML) rather than a console
		:param verbose: every message is shown, not only the notable ones
		:param prefix: the log file's name starts with it
		:param job_id: a web job's id - names the file and its live log
		:param redis_client: where a web job's live log is (None for the CLI)"""
		self._log_lock = threading.Lock()
		self._verbose = verbose

		ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
		logs_dir = runtime.logs_dir()
		os.makedirs(logs_dir, exist_ok=True)
		name = f"{prefix}_{ts}_{job_id}.log" if job_id else f"{prefix}_{ts}.log"
		self.logfile = os.path.join(logs_dir, name)

		# a live log needs a job and a Redis; a web logger without one shows
		# nothing (its messages are in the file only)
		self.live_log = LiveLog(redis_client, job_id) \
			if job_id and redis_client is not None else None
		self._echo: Echo | None = self.live_log if webapp else Console()
		self._dress = LiveLog.dress if webapp else Console.dress

	def _log(self, message: str) -> None:
		"""Append a timestamped line to the log file (threads take turns)."""
		with self._log_lock:
			with open(self.logfile, "a", encoding="utf-8") as file:
				timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
				file.write(f"{timestamp}\t{message}\n")

	def _msg(self, message: str, color: str = Tone.INFO) -> str:
		"""The message dressed for where it's shown: an HTML <div> with the
		page's colour class (the text escaped), or ANSI colours for a console.

		:param color: a Tone (its colour name); Tone.INFO for none"""
		return self._dress(message, color)

	def notify(self, message: str, color: str = Tone.INFO, important: bool = False) -> \
			None:
		"""Log a message. Every message goes to the file; the notable ones -
		errors (red), important ones, or all in verbose mode - are also shown:
		a web job's in its live log, the CLI's on the console.

		The file comes first; the live log is best-effort - a Redis outage
		must not fail the rollout (and lose its results) over a log line.

		:param color: a Tone - ERROR (always shown), WARNING, SUCCESS; INFO for none
		:param important: shown even when not verbose"""
		self._log(message)
		if self._echo is not None and (important or self._verbose or color == Tone.ERROR):
			self._echo.show(message, color)

	def get_history(self) -> list[str]:
		""":returns: the live log so far (LiveLog.history); [] without one"""
		return self.live_log.history() if self.live_log else []

	def subscribe(self) -> "PubSub":
		"""LiveLog.subscribe.

		:raises RuntimeError: this logger has no live log (no Redis client or
		 job id) - there is nothing to subscribe to"""
		if self.live_log is None:
			raise RuntimeError("this logger has no live log (no Redis client or job id)")
		return self.live_log.subscribe()

	def follow(self, over: Callable[[], bool], wait: float = 0.5) -> Iterator[str | None]:
		"""LiveLog.follow; nothing without a live log."""
		return self.live_log.follow(over, wait) if self.live_log else iter(())

	def redis_cleanup(self) -> None:
		"""The job is over: LiveLog.close (nothing without a live log)."""
		if self.live_log:
			self.live_log.close()
