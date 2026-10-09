"""The app's side of the port helper contract (src/setup/port.py has the
helper's; overview: docs/architecture.md §9).

The HTTPS port is published by Docker, so a new one needs nginx recreated —
done on the host by the port helper, never by the app (no Docker access
here). The app only writes requests and reads the helper's answers:

- site.env (src/access/site_env.py)  app → helper: the port wanted, a request id and
                              its time; the id again once an admin's browser
                              reached NetRollout through the new port
- config/apply-status.json    helper → app: trying / applied / rolled_back /
                              failed

The System Settings value is the *desired* port; anything that shows or
redirects to an address uses serving_port()."""
import dataclasses
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from src import runtime
from src.access import site_env
from src.db.settings import SETTINGS


STATUS_FILE = "apply-status.json"
_REQUEST_KEYS = (site_env.PORT_REQUEST, site_env.PORT_REQUEST_ID,
                 site_env.PORT_REQUESTED_AT, site_env.PORT_CONFIRMED)
# compose passes .env's HTTPS_PORT (what Docker published when this
# container was created) under this name
PUBLISHED_PORT_ENV = "NETROLLOUT_HTTPS_PORT"
# the page stops waiting for a helper that doesn't pick a request up
WAIT_SECONDS = 30


class ApplyState(StrEnum):
	"""apply-status.json's state: the helper's answer (src/setup/port.py
	writes it)."""
	TRYING = "trying"            # both ports open: confirm from the new one
	APPLIED = "applied"          # the port in use is the one asked for
	ROLLED_BACK = "rolled_back"  # not confirmed in time, or superseded
	FAILED = "failed"            # refused (the reason in "message")
	UNKNOWN = "unknown"          # the app's reading of a file it can't read


class PortPageState(StrEnum):
	"""What System Settings shows about the port (state())."""
	APPLIED = "applied"                    # nothing pending
	MANUAL = "manual"                      # no helper: run `netrollout apply`
	WAITING = "waiting"                    # the helper hasn't picked it up
	NO_HELPER_ANSWER = "no_helper_answer"  # ... within WAIT_SECONDS
	TRYING = "trying"                      # confirm from the new port
	CONFIRMING = "confirming"              # confirmed, the old port being dropped
	ROLLED_BACK = "rolled_back"
	FAILED = "failed"


@dataclass(frozen=True)
class ApplyStatus:
	"""apply-status.json: the helper's last answer - written by the helper
	(src/setup/port.py), read here. The values are the file's, as written
	(each reader checks what it uses); a key missing from the file is its
	default."""
	id: str = ""                  # the request it answers
	state: str | None = None      # an ApplyState
	port: Any = None              # the port in use
	trying: Any = None            # a trial's new port
	deadline: Any = None          # when the trial ends (epoch seconds)
	message: Any = ""             # why (a rollback, a failure)
	time: Any = None              # when it was written (local, ISO 8601)

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> "ApplyStatus":
		""":returns: the status the file's object holds (other keys ignored)"""
		names = {f.name for f in dataclasses.fields(cls)}
		return cls(**{k: v for k, v in data.items() if k in names})

	def as_dict(self) -> dict[str, Any]:
		""":returns: the file's object - every key, in the file's order"""
		return dataclasses.asdict(self)


def _path(name: str) -> Path:
	""":returns: the file in the config folder"""
	return runtime.config_dir() / name


def _port(value: object) -> int | None:
	""":returns: the value as a valid HTTPS port; None when it isn't one"""
	try:
		return cast(int, SETTINGS["https_port"].parse(value))
	except (ValueError, TypeError):
		return None


def read_status() -> ApplyStatus | None:
	"""The helper's last answer; None when no helper reports here (dev,
	before stage 9, your own reverse proxy). Unreadable → state "unknown"."""
	try:
		data = json.loads(_path(STATUS_FILE).read_text(encoding="utf-8"))
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		return ApplyStatus(state=ApplyState.UNKNOWN)
	return ApplyStatus.from_dict(data) if isinstance(data, dict) \
		else ApplyStatus(state=ApplyState.UNKNOWN)


def read_request() -> dict[str, Any] | None:
	"""The pending request as {"port", "id", "time"}; None when there is
	none."""
	try:
		values = site_env.read()
	except OSError:
		return None
	if not values.get(site_env.PORT_REQUEST_ID):
		return None
	try:
		written = float(values.get(site_env.PORT_REQUESTED_AT, ""))
	except ValueError:
		written = 0.0
	return {"port": _port(values.get(site_env.PORT_REQUEST)),
	        "id": values[site_env.PORT_REQUEST_ID], "time": written}


def _confirmed_id() -> str:
	""":returns: the request id an admin's browser confirmed ("" none)"""
	try:
		return site_env.read().get(site_env.PORT_CONFIRMED, "")
	except OSError:
		return ""


def serving_port() -> int:
	"""The port people reach NetRollout on right now: the helper's (a trial
	the admin already confirmed counts), else what Docker published when this
	container started, else 443."""
	status = read_status()
	if status:
		if (status.state == ApplyState.TRYING and status.id
				and status.id == _confirmed_id()
				and (trial := _port(status.trying))):
			return trial
		if port := _port(status.port):
			return port
	return published_port()


def published_port() -> int:
	""":returns: the port Docker published when this container was created
	 (PUBLISHED_PORT_ENV); 443 when unset or not a port"""
	return _port(os.environ.get(PUBLISHED_PORT_ENV, "443")) or 443


def request_port(port: int) -> Callable[[], None]:
	"""Ask for `port` (a new request id each time — also for "Try again").

	:returns: undo(), which puts the previous request back
	:raises ValueError: an invalid port
	:raises OSError: site.env can't be written"""
	port = cast(int, SETTINGS["https_port"].parse(port))
	current = site_env.read()
	previous = {k: current.get(k) for k in _REQUEST_KEYS}
	site_env.update({site_env.PORT_REQUEST: str(port),
	                 site_env.PORT_REQUEST_ID: secrets.token_hex(8),
	                 site_env.PORT_REQUESTED_AT: str(int(time.time())),
	                 site_env.PORT_CONFIRMED: None})

	def undo() -> None:
		site_env.update(previous)
	return undo


def confirm(apply_id: str, reached_port: int) -> str | None:
	"""An admin's browser reached NetRollout on `reached_port` during the
	trial `apply_id`: tell the helper to keep the new port. (`reached_port`
	comes from the request's Host header — it can only fool the admin sending
	it, which proves nothing to anyone else anyway.)

	:returns: None when confirmed, else why it can't be"""
	status = read_status() or ApplyStatus()
	if status.state != ApplyState.TRYING or not apply_id or status.id != apply_id:
		return "There is no port change waiting for confirmation."
	if _port(status.trying) != reached_port:
		return (f"Open NetRollout on port {status.trying} to confirm "
		        f"— this page came through port {reached_port}.")
	if status.deadline and time.time() > float(status.deadline):
		return "Too late: the trial ended, the previous port is back."
	site_env.update({site_env.PORT_CONFIRMED: apply_id})
	return None


def state(saved_port: int) -> dict[str, Any]:
	"""What the page shows about the port (the setting holds `saved_port`):
	{"state", "saved", "serving", ...}. States: applied (nothing pending),
	manual (no helper: run `netrollout apply`), waiting (the helper hasn't
	picked it up), no_helper_answer (it didn't within WAIT_SECONDS), trying
	(both ports open: confirm from the new one), confirming (confirmed, the
	helper is dropping the old port), rolled_back / failed (the helper's
	reason in "message")."""
	serving = serving_port()
	out: dict[str, Any] = {"saved": saved_port, "serving": serving}
	status = read_status()
	request = read_request()
	if status and request and request["id"] and status.id == request["id"]:
		st = status.state
		if st == ApplyState.TRYING and _confirmed_id() == request["id"]:
			return {**out, "state": PortPageState.CONFIRMING}
		if st == ApplyState.TRYING:
			return {**out, "state": PortPageState.TRYING, "id": request["id"],
			        "trying": _port(status.trying), "deadline": status.deadline}
		if st in (ApplyState.ROLLED_BACK, ApplyState.FAILED):
			return {**out, "state": PortPageState(st), "message": status.message}
	if saved_port == serving:
		return {**out, "state": PortPageState.APPLIED}
	if status is None:
		return {**out, "state": PortPageState.MANUAL}
	if request and request["port"] == saved_port \
			and time.time() - request["time"] > WAIT_SECONDS:
		return {**out, "state": PortPageState.NO_HELPER_ANSWER}
	return {**out, "state": PortPageState.WAITING}
