"""What nginx serves, from the app's side.

The app writes two values — the hostname (System Settings) and the HTTPS port
that is actually published — into site.env in the folder it shares with nginx
(config/nginx). The nginx image's watcher (deploy/nginx/) validates them,
renders its own template, tests the result with `nginx -t` and reloads — or
keeps serving the last good site — and reports in status.json. The app never
writes nginx syntax.
"""
import datetime
import json
import os
import tempfile
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from src import runtime
from src.db.settings import SETTINGS

SITE_FILE = "site.env"
STATUS_FILE = "status.json"
# compose passes .env's HTTPS_PORT (what Docker publishes) under this name
APPLIED_PORT_ENV = "NETROLLOUT_HTTPS_PORT"


def shared_dir() -> Path:
	return runtime.config_dir() / "nginx"


def applied_https_port() -> int:
	"""The port Docker publishes — not the System Settings value, which waits
	for `netrollout apply`: until then redirects must keep using this one."""
	try:
		return SETTINGS["https_port"].parse(os.environ.get(APPLIED_PORT_ENV, "443"))
	except ValueError:
		return 443


def write_site(hostname: str | None) -> bool:
	"""Write site.env for `hostname` (empty: no canonical name). True if it
	changed — an unchanged file isn't rewritten, so nginx isn't reloaded for
	nothing. Raises ValueError for an invalid hostname, OSError when the
	folder can't be written."""
	host = SETTINGS["public_hostname"].parse(hostname or "")
	content = (f"NETROLLOUT_HOSTNAME={host}\n"
	           f"NETROLLOUT_HTTPS_PORT={applied_https_port()}\n")
	folder = shared_dir()
	folder.mkdir(parents=True, exist_ok=True)
	target = folder / SITE_FILE
	if target.is_file() and target.read_text(encoding="utf-8") == content:
		return False
	# a temp file + rename: the watcher sees the old file or the new one
	fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{SITE_FILE}.")
	try:
		with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
			f.write(content)
		os.chmod(tmp, 0o644)
		os.replace(tmp, target)
	except BaseException:
		if os.path.exists(tmp):
			os.remove(tmp)
		raise
	return True


def read_status() -> dict | None:
	"""The watcher's last verdict: {"state": "applied" | "rejected",
	"message", "time"}. None when no nginx reports here (an external proxy,
	or dev without the stack) — "not managed"."""
	try:
		data = json.loads((shared_dir() / STATUS_FILE).read_text(encoding="utf-8"))
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		return {"state": "unknown", "message": "nginx's status.json can't be read",
		        "time": None}
	if not isinstance(data, dict):
		return {"state": "unknown", "message": "nginx's status.json can't be read",
		        "time": None}
	return data


def _status_time(status: dict) -> float | None:
	try:
		return datetime.datetime.strptime(status["time"], "%Y-%m-%dT%H:%M:%SZ") \
			.replace(tzinfo=datetime.timezone.utc).timestamp()
	except (KeyError, TypeError, ValueError):
		return None


def wait_for_status(after: float, timeout: float = 8.0,
                    poll: float = 0.5) -> dict | None:
	"""The first verdict the watcher writes at or after `after` (epoch
	seconds; it checks every 3 s), or None if none comes within `timeout`."""
	deadline = time.monotonic() + timeout
	while True:
		status = read_status()
		written = _status_time(status) if status else None
		# the watcher's clock has whole seconds
		if written is not None and written >= int(after):
			return status
		if time.monotonic() >= deadline:
			return None
		time.sleep(poll)


def sync_at_start(settings) -> None:
	"""At every start nginx gets the saved hostname — also after a change made
	while it was down, or a restore. Never stops the app from starting."""
	try:
		changed = write_site(settings.get("public_hostname"))
	except (ValueError, OSError, SQLAlchemyError) as e:
		print(f"[NetRollout] nginx site values not written ({shared_dir()}): "
		      f"{e}", flush=True)
		return
	if changed:
		print(f"[NetRollout] nginx site values written ({shared_dir() / SITE_FILE})",
		      flush=True)
