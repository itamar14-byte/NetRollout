"""The app's side of the port helper contract (docs/plans/phase-4.md, stage 9).

The HTTPS port is published by Docker, so a new one needs nginx recreated —
done on the host by the port helper, never by the app (no Docker access
here). The app only writes requests and reads the helper's answers, all as
files in config/:

- desired.env        app → helper: the port wanted, with a request id
- apply-status.json  helper → app: trying / applied / rolled_back / failed
- apply-confirm      app → helper: the id, once an admin's browser reached
                     NetRollout through the new port

The System Settings value is the *desired* port; anything that shows or
redirects to an address uses serving_port()."""
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from src import runtime
from src.db.settings import SETTINGS

DESIRED_FILE = "desired.env"
STATUS_FILE = "apply-status.json"
CONFIRM_FILE = "apply-confirm"
# compose passes .env's HTTPS_PORT (what Docker published when this
# container was created) under this name
PUBLISHED_PORT_ENV = "NETROLLOUT_HTTPS_PORT"
# the page stops waiting for a helper that doesn't pick a request up
WAIT_SECONDS = 30


def _path(name: str) -> Path:
	return runtime.config_dir() / name


def _port(value) -> int | None:
	try:
		return SETTINGS["https_port"].parse(value)
	except (ValueError, TypeError):
		return None


def _write(name: str, content: str) -> None:
	"""Atomically: the helper sees the old file or the new one."""
	folder = runtime.config_dir()
	folder.mkdir(parents=True, exist_ok=True)
	fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{name}.")
	try:
		with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
			f.write(content)
		os.chmod(tmp, 0o644)
		os.replace(tmp, folder / name)
	except OSError as e:
		Path(tmp).unlink(missing_ok=True)
		# name the file people know, not the temp file
		raise OSError(e.errno, e.strerror, str(folder / name)) from e
	except BaseException:
		Path(tmp).unlink(missing_ok=True)
		raise


def read_status() -> dict | None:
	"""The helper's last answer; None when no helper reports here (dev,
	before stage 9, your own reverse proxy). Unreadable → state "unknown"."""
	try:
		data = json.loads(_path(STATUS_FILE).read_text(encoding="utf-8"))
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		return {"state": "unknown"}
	return data if isinstance(data, dict) else {"state": "unknown"}


def read_request() -> dict | None:
	"""desired.env as {"port", "id", "time"}; None when there is none."""
	path = _path(DESIRED_FILE)
	try:
		lines = path.read_text(encoding="utf-8").splitlines()
		written = path.stat().st_mtime
	except OSError:
		return None
	values = dict(line.split("=", 1) for line in lines if "=" in line)
	return {"port": _port(values.get("NETROLLOUT_HTTPS_PORT")),
	        "id": values.get("NETROLLOUT_APPLY_ID", ""), "time": written}


def _confirmed_id() -> str:
	try:
		return _path(CONFIRM_FILE).read_text(encoding="utf-8").strip()
	except OSError:
		return ""


def serving_port() -> int:
	"""The port people reach NetRollout on right now: the helper's (a trial
	the admin already confirmed counts), else what Docker published when this
	container started, else 443."""
	status = read_status()
	if status:
		if (status.get("state") == "trying" and status.get("id")
				and status.get("id") == _confirmed_id()
				and _port(status.get("trying"))):
			return _port(status["trying"])
		if _port(status.get("port")):
			return _port(status["port"])
	return _port(os.environ.get(PUBLISHED_PORT_ENV, "443")) or 443


def request_port(port: int):
	"""Ask for `port` (a new request id each time — also for "Try again").
	Returns undo(), which puts the previous request back. Raises ValueError
	for an invalid port, OSError when the file can't be written."""
	port = SETTINGS["https_port"].parse(port)
	path = _path(DESIRED_FILE)
	previous = path.read_bytes() if path.is_file() else None
	_write(DESIRED_FILE, f"NETROLLOUT_HTTPS_PORT={port}\n"
	                     f"NETROLLOUT_APPLY_ID={secrets.token_hex(8)}\n")

	def undo():
		if previous is None:
			path.unlink(missing_ok=True)
		else:
			_write(DESIRED_FILE, previous.decode("utf-8"))
	return undo


def confirm(apply_id: str, reached_port: int) -> str | None:
	"""An admin's browser reached NetRollout on `reached_port` during the
	trial `apply_id`: tell the helper to keep the new port. Returns None, or
	why it can't be confirmed. (`reached_port` comes from the request's Host
	header — it can only fool the admin sending it, which proves nothing to
	anyone else anyway.)"""
	status = read_status() or {}
	if status.get("state") != "trying" or not apply_id \
			or status.get("id") != apply_id:
		return "There is no port change waiting for confirmation."
	if _port(status.get("trying")) != reached_port:
		return (f"Open NetRollout on port {status.get('trying')} to confirm "
		        f"— this page came through port {reached_port}.")
	if status.get("deadline") and time.time() > float(status["deadline"]):
		return "Too late: the trial ended, the previous port is back."
	_write(CONFIRM_FILE, apply_id + "\n")
	return None


def state(saved_port: int) -> dict:
	"""What the page shows about the port (the setting holds `saved_port`):
	{"state", "saved", "serving", ...}. States: applied (nothing pending),
	manual (no helper: run `netrollout apply`), waiting (the helper hasn't
	picked it up), no_helper_answer (it didn't within WAIT_SECONDS), trying
	(both ports open: confirm from the new one), confirming (confirmed, the
	helper is dropping the old port), rolled_back / failed (the helper's
	reason in "message")."""
	serving = serving_port()
	out = {"saved": saved_port, "serving": serving}
	status = read_status()
	request = read_request()
	if status and request and request["id"] and status.get("id") == request["id"]:
		st = status.get("state")
		if st == "trying" and _confirmed_id() == request["id"]:
			return {**out, "state": "confirming"}
		if st == "trying":
			return {**out, "state": "trying", "id": request["id"],
			        "trying": _port(status.get("trying")),
			        "deadline": status.get("deadline")}
		if st in ("rolled_back", "failed"):
			return {**out, "state": st, "message": status.get("message", "")}
	if saved_port == serving:
		return {**out, "state": "applied"}
	if status is None:
		return {**out, "state": "manual"}
	if request and request["port"] == saved_port \
			and time.time() - request["time"] > WAIT_SECONDS:
		return {**out, "state": "no_helper_answer"}
	return {**out, "state": "waiting"}
