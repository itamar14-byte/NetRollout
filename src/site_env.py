"""config/nginx/site.env — what NetRollout hands the components outside it.

One file, one module writing it: keys are read and updated together under a
lock and the file is replaced whole (atomically), so no reader ever sees half
of a change. Each reader takes only its own keys:

- nginx (deploy/nginx): HOSTNAME and HTTPS_PORT — the port in use, never the
  one requested; it validates them and ignores every other key
- the port helper (host side, stage 9): the PORT_REQUEST keys
- the app, at its first start: HOSTNAME, written by the installer, seeds the
  System Settings hostname

No web dependencies: the installer's setup core uses it too."""
import os
import tempfile
import threading
from pathlib import Path

from src import runtime

FILE = "site.env"

HOSTNAME = "NETROLLOUT_HOSTNAME"
HTTPS_PORT = "NETROLLOUT_HTTPS_PORT"            # in use (published)
PORT_REQUEST = "NETROLLOUT_PORT_REQUEST"        # wanted (System Settings)
PORT_REQUEST_ID = "NETROLLOUT_PORT_REQUEST_ID"  # a new id per request
PORT_REQUESTED_AT = "NETROLLOUT_PORT_REQUESTED_AT"   # epoch seconds
PORT_CONFIRMED = "NETROLLOUT_PORT_CONFIRMED"    # the id, confirmed from the new port

# written in this order (the rest, if any, after them)
_ORDER = (HOSTNAME, HTTPS_PORT, PORT_REQUEST, PORT_REQUEST_ID,
          PORT_REQUESTED_AT, PORT_CONFIRMED)
_lock = threading.Lock()


def folder() -> Path:
	"""Shared with nginx (mounted into its container)."""
	return runtime.config_dir() / "nginx"


def path() -> Path:
	return folder() / FILE


def read() -> dict[str, str]:
	"""Every key; {} when there's no file. A key's last value wins."""
	try:
		text = path().read_text(encoding="utf-8")
	except FileNotFoundError:
		return {}
	values = {}
	for line in text.splitlines():
		key, sep, value = line.partition("=")
		if sep and key.strip():
			values[key.strip()] = value.strip()
	return values


def update(values: dict[str, str | None]) -> bool:
	"""Set keys (None removes one); the others are kept. True if the file
	changed — an unchanged file isn't rewritten, so nginx isn't reloaded for
	nothing. Raises OSError when the folder can't be written (the message
	names site.env, not the temporary file)."""
	with _lock:
		current = read()
		new = dict(current)
		for key, value in values.items():
			if value is None:
				new.pop(key, None)
			else:
				new[key] = str(value)
		if new == current and path().is_file():
			return False
		keys = [k for k in _ORDER if k in new] + \
		       sorted(k for k in new if k not in _ORDER)
		_write("".join(f"{k}={new[k]}\n" for k in keys))
		return True


def _write(content: str) -> None:
	target = path()
	target.parent.mkdir(parents=True, exist_ok=True)
	fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{FILE}.")
	try:
		with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
			f.write(content)
		os.chmod(tmp, 0o644)
		os.replace(tmp, target)
	except OSError as e:
		Path(tmp).unlink(missing_ok=True)
		raise OSError(e.errno, e.strerror, str(target)) from e
	except BaseException:
		Path(tmp).unlink(missing_ok=True)
		raise
