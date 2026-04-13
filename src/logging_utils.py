import datetime
import html
import os
import queue
import threading

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
                 job_id: str = None, timestamp: str = None):
        self._queue = queue.Queue()
        self._buffer = []
        self._buffer_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._webapp = webapp
        self._verbose = verbose
        if job_id and timestamp:
            os.makedirs(LOGS_DIR, exist_ok=True)
            self.logfile = os.path.join(LOGS_DIR,
                                        f"rollout_{timestamp}_{job_id}.log")
        else:
            self.logfile = datetime.datetime.now().strftime(
                "rollout_%Y%m%d_%H%M%S.log")

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
            if important or self._verbose or color == "red":
                content = self._msg(message, color)
                self._queue.put(content)
                with self._buffer_lock:
                    self._buffer.append(content)
            self._log(message)
            return None
        else:
            if important or self._verbose or color == "red":
                print(self._msg(message, color))
            self._log(message)

    def get_buffer_snapshot(self) -> list[str]:
        with self._buffer_lock:
            return list(self._buffer)

    def get_queue(self, timeout: int) -> str:
        return self._queue.get(timeout=timeout)

