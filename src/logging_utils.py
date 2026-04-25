import datetime
import html
import os
import threading

from redis.client import PubSub

from db.redis_db import redis_client

LOGS_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


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
                 prefix: str = "rollout", job_id: str = None):
        self._log_lock = threading.Lock()
        self._webapp = webapp
        self._verbose = verbose

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
            with open(self.logfile, "a") as file:
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
                redis_client.rpush(self._history_key, content)
                redis_client.publish(self._channel_key, content)
            self._log(message)
            return None
        else:
            if important or self._verbose or color == "red":
                print(self._msg(message, color))
            self._log(message)

    def get_history(self) -> list[str]:
        return [m.decode() for m in redis_client.lrange(self._history_key, 0, -1)]

    def subscribe(self) -> PubSub:
        ps = redis_client.pubsub()
        ps.subscribe(self._channel_key)
        return ps

    def redis_cleanup(self) -> None:
        redis_client.publish(self._channel_key, "__done__")
        redis_client.delete(self._history_key)
        redis_client.delete(self._channel_key)


