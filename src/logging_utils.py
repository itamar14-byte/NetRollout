import datetime
import queue


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
    def __init__(self, webapp: bool, verbose: bool, logfile: str = None):
        self._queue = queue.Queue()
        self._webapp = webapp
        self._verbose = verbose
        self.logfile = (logfile or datetime.datetime.now().
                        strftime("rollout_%Y%m%d_%H%M%S._log"))

    def _log(self, message: str) -> None:
        """
        A logging function that writes a message to a logfile with
         the globally configured name and attaches the message to a timestamp
        :param message: message to write in the _log
        """

        with open(self.logfile, "a") as file:
            # Sets the current timestamp for the time of call and adds the stamped message to the _log file
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            file.write(f"{timestamp}\t{message}\n")

    def _msg(self, message: str, color: str = "") -> str:
        """Adds ANSI escape sequences to terminal color for progress and error messages"""
        if self._webapp:
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


    def notify(self, message: str, color: str = "") -> None:
        """A wrapper logging function.
        	 All messages are logged to the file.
        	Additionally, error messages, or messages generated in _verbose mode are printed to console
        	"""
        if self._webapp:
            if self._verbose or color == "red":
                self._queue.put(self._msg(message, color))
            self._log(message)
            return None
        else:
            if self._verbose or color == "red":
                print(self._msg(message, color))
            self._log(message)

    def get(self) -> str:
        return self._queue.get(timeout=1)
