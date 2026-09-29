import datetime
import html
import os
import sys
import threading
import time

import redis
from redis.client import PubSub

LOGS_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")

# Log files are kept longer than job records (db_install.JOB_RETENTION_DAYS,
# which bounds Download Log on Results): the logs folder is browsed directly
# for older troubleshooting. Must stay >= the job retention.
LOG_RETENTION_DAYS = 60
LOG_PRUNE_INTERVAL_HOURS = 24


def prune_logs(retention_days: int = LOG_RETENTION_DAYS,
               logs_dir: str = LOGS_DIR) -> int:
    """Delete *.log files (web app and CLI alike) not modified for
    `retention_days`. Uses the last-modified time, so a running job's file —
    still being appended to — is never removed. Files that can't be removed
    (locked, permissions) are skipped. :return: number of files removed"""
    cutoff = time.time() - retention_days * 86400
    removed = 0
    try:
        entries = os.scandir(logs_dir)
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


def start_log_pruning() -> None:
    """Prune now, then every LOG_PRUNE_INTERVAL_HOURS from a daemon thread —
    a server that never restarts still cleans up. Called by the web app's
    entry point."""
    def loop():
        while True:
            removed = prune_logs()
            if removed:
                print(f"[NetRollout] Removed {removed} log file(s) older than "
                      f"{LOG_RETENTION_DAYS} days", flush=True)
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
    def __init__(self, webapp: bool, verbose: bool,
                 prefix: str = "rollout", job_id: str = None,
                 redis_client: redis.Redis | None = None):
        self._log_lock = threading.Lock()
        self._webapp = webapp
        self._verbose = verbose
        self._redis = redis_client

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(LOGS_DIR, exist_ok=True)
        if job_id:
            self.logfile = os.path.join(LOGS_DIR,
                                        f"{prefix}_{ts}_{job_id}.log")
            self._channel_key = f"job:{job_id}:logs"
            self._history_key = f"job:{job_id}:history"

        else:
            self.logfile = os.path.join(LOGS_DIR,
                                        f"{prefix}_{ts}.log")
            self._channel_key, self._history_key = None, None

    def _log(self, message: str) -> None:
        """
        A logging function that writes a message to a logfile with
         the globally configured name and attaches the message to a timestamp
        :param message: message to write in the _log
        """

        with self._log_lock:
            with open(self.logfile, "a", encoding="utf-8") as file:
                # Sets the current timestamp for the time of call and adds the stamped message to the _log file
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                file.write(f"{timestamp}\t{message}\n")

    def _msg(self, message: str, color: str = "") -> str:
        """Adds ANSI escape sequences to terminal color for progress and error messages"""
        if self._webapp:
            message = html.escape(message)
            color = ANSI_TO_HTML.get(color.upper()) if color else None
            if color:
                return color + message + WEBAPP_END
            return message
        else:
            if color:
                color = COLORS.get(color.upper())
                if color:
                    return color + message + END
            return message


    def notify(self, message: str, color: str = "", important: bool = False) -> \
            None:
        """A wrapper logging function.
        	 All messages are logged to the file.
        	Additionally, error messages, or messages generated in _verbose mode are printed to console
        	"""
        if self._webapp:
            if (important or self._verbose or color == "red") and self._channel_key:
                content = self._msg(message, color)
                self._redis.rpush(self._history_key, content)
                self._redis.publish(self._channel_key, content)
            self._log(message)
            return None
        else:
            if important or self._verbose or color == "red":
                print(self._msg(message, color))
            self._log(message)

    def get_history(self) -> list[str]:
        return [m.decode() for m in self._redis.lrange(self._history_key, 0, -1)]

    def subscribe(self) -> PubSub:
        ps = self._redis.pubsub()
        ps.subscribe(self._channel_key)
        return ps

    def redis_cleanup(self) -> None:
        self._redis.publish(self._channel_key, "__done__")
        self._redis.delete(self._history_key)
        self._redis.delete(self._channel_key)


