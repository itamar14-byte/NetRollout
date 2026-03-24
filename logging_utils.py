import datetime
import os
import queue

# Creates a timestamp for the defined globally at every running of Core.py.
# Variable will be calculated when importing helper
LOGFILE = datetime.datetime.now().strftime("rollout_%Y%m%d_%H%M%S.log")
LOG_QUEUE = queue.Queue()
BASEDIR = os.path.abspath(os.path.dirname(__file__))

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



def msg(string: str, color: str = "", webapp: bool = False) -> str:
    """Adds ANSI escape sequences to terminal color for progress and error messages"""
    if webapp:
        if color:
            color = ANSI_TO_HTML.get(color.upper())
            return color + string + WEBAPP_END
        return string
    else:
        if color:
            color = COLORS.get(color.upper())
            return color + string + END
        return string


def log(string: str, file_name: str = LOGFILE) -> None:
    """
    A logging function that writes a message to a logfile with
     the globally configured name and attaches the message to a timestamp
    :param file_name: TIme stamped name for the logfile that is written too when log is called
    :param string: message to write in the log
    """
    with open(file_name, "a") as file:
        # Sets the current timestamp for the time of call and adds the stamped message to the log file
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        file.write(f"{timestamp}\t{string}\n")


def base_notify(string: str, color: str = None,
                verbose: bool = False, webapp: bool = False) -> None:
    """A wrapper logging function.
	 All messages are logged to the file.
	Additionally, error messages, or messages generated in verbose mode are printed to console
	"""
    if webapp:
        if verbose:
            LOG_QUEUE.put(msg(string, color, webapp=True))
        log(string)
        return None
    else:
        if verbose:
            print(msg(string, color))
        log(string)