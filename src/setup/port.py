"""The port helper's thinking (the HTTPS port changed in System Settings):
the scripts do what needs Docker, this decides and records. The contract
with the app is in docs/plans/stage-9.md and src/webapp/port_apply.py:

  site.env (src/site_env.py)  app -> helper: the port wanted, a request id,
                              its time; the id again once an admin's browser
                              reached NetRollout through the new port
  config/apply-status.json    helper -> app: {"id", "state", "port",
                              "trying", "deadline", "message", "time"}

A pass (`next_step`) answers one of:
  none      nothing to do (no request, handled already, or refused - then
            recorded as failed here, with the reason)
  wait      a trial runs; not confirmed yet, the deadline not passed
  try P     open port P next to the current one: `open_trial` (a compose
            file adding P, listed in .env's COMPOSE_FILE so every compose
            call keeps it during the trial), nginx recreated, `trying`
  keep      confirmed from the new port: `close("keep")` - .env's
            HTTPS_PORT, the trial file gone, nginx recreated on P only
  rollback  not confirmed in time, superseded, or a leftover from a helper
            that died: `close("rollback")` - nginx back on the old port
Each request id is handled once; a rolled-back id is never retried by
itself (the page's Try again makes a new one).
"""
import datetime
import json
import os
from dataclasses import dataclass
from pathlib import Path

from src import runtime, site_env
from src.setup import files, manage

STATUS_FILE = "apply-status.json"          # src/webapp/port_apply.py reads it
TRIAL_FILE = "port-trial.yaml"             # in config/, listed in COMPOSE_FILE
TRIAL_ENTRY = "config/" + TRIAL_FILE       # relative to the install folder
TRIAL_SECONDS = 120                        # time to click through a certificate warning


@dataclass
class Step:
	action: str               # none / wait / try / keep / rollback
	port: int | None = None   # try: the new port
	id: str = ""
	message: str = ""         # what to say (refused, rolled back: why)


def status_path() -> Path:
	return runtime.config_dir() / STATUS_FILE


def trial_path() -> Path:
	return runtime.config_dir() / TRIAL_FILE


def read_status() -> dict | None:
	try:
		data = json.loads(status_path().read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None
	return data if isinstance(data, dict) else None


def write_status(id_: str, state: str, port: int, trying: int | None = None,
                 deadline: float | None = None, message: str = "",
                 now: float | None = None) -> dict:
	now = now if now is not None else datetime.datetime.now().timestamp()
	status = {"id": id_, "state": state, "port": port, "trying": trying,
	          "deadline": deadline, "message": message,
	          "time": datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds")}
	path = status_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_name(path.name + ".tmp")
	tmp.write_text(json.dumps(status), encoding="utf-8")
	os.replace(tmp, path)
	return status


def current_port() -> int:
	"""The port Docker publishes now (.env's HTTPS_PORT)."""
	try:
		return int(manage.env_read().get("HTTPS_PORT", "443"))
	except ValueError:
		return 443


def _wanted(raw: str | None) -> int | None:
	try:
		port = int(str(raw).strip())
	except (TypeError, ValueError):
		return None
	return port if 1 <= port <= 65535 and port != 80 else None


def next_step(busy: dict[int, str], now: float | None = None) -> Step:
	"""What the helper does now. `busy`: ports listening on this computer,
	NetRollout's own published ones left out. A request that can't be tried
	is recorded as failed here (the page shows the reason)."""
	now = now if now is not None else datetime.datetime.now().timestamp()
	request = site_env.read()
	req_id = request.get(site_env.PORT_REQUEST_ID, "")
	status = read_status() or {}
	current = current_port()

	# a trial in progress: its own outcome first
	if status.get("state") == "trying":
		trial_id = status.get("id", "")
		if trial_id != req_id:
			return Step("rollback", id=trial_id,
			            message="replaced by a newer request")
		if request.get(site_env.PORT_CONFIRMED) == req_id:
			return Step("keep", status.get("trying"), req_id)
		if now > float(status.get("deadline") or 0):
			return Step("rollback", id=req_id,
			            message=f"not confirmed within {TRIAL_SECONDS} s - port "
			                    f"{status.get('trying')} didn't open from a browser "
			                    f"(a firewall?)")
		return Step("wait", status.get("trying"), req_id)

	if not req_id or status.get("id") == req_id:
		return Step("none")                       # nothing asked, or handled
	port = _wanted(request.get(site_env.PORT_REQUEST))
	if port is None:
		message = f"{request.get(site_env.PORT_REQUEST)!r} isn't a usable HTTPS port"
	elif port == current:
		write_status(req_id, "applied", current, now=now)
		return Step("none", current, req_id, f"port {current} is in use already")
	elif port in busy:
		who = busy[port]
		message = f"port {port} is in use on this computer{f' (by {who})' if who else ''}"
	else:
		return Step("try", port, req_id)
	write_status(req_id, "failed", current, message=message, now=now)
	return Step("none", port, req_id, message)


def _compose_files() -> list[str]:
	env = manage.env_read()
	return [f for f in env.get("COMPOSE_FILE", files.COMPOSE).split(",") if f]


def open_trial(port: int) -> None:
	"""The trial's compose file (port P next to the current one) and its
	place in COMPOSE_FILE - every compose call keeps it during the trial."""
	trial_path().parent.mkdir(parents=True, exist_ok=True)
	trial_path().write_text(
		"# NetRollout's port helper: a trial of a new HTTPS port, next to the\n"
		"# current one - removed when it's kept or rolled back\n"
		"services:\n  nginx:\n    ports:\n"
		f'      - "{port}:443"\n', encoding="utf-8")
	compose = _compose_files()
	if TRIAL_ENTRY not in compose:
		manage.env_set({"COMPOSE_FILE": ",".join(compose + [TRIAL_ENTRY])})


def trying(port: int, id_: str, now: float | None = None) -> dict:
	"""nginx answers on both ports: the trial's clock starts."""
	now = now if now is not None else datetime.datetime.now().timestamp()
	return write_status(id_, "trying", current_port(), trying=port,
	                    deadline=now + TRIAL_SECONDS, now=now)


def close(outcome: str, id_: str, message: str = "",
          now: float | None = None) -> dict:
	"""The trial ends: keep (the new port in .env and site.env), rollback or
	failed (the current port stays). The trial file goes either way; then
	the script recreates nginx."""
	status = read_status() or {}
	new = status.get("trying")
	if outcome == "keep" and new:
		manage.env_set({"HTTPS_PORT": str(new)})
		site_env.update({site_env.HTTPS_PORT: str(new)})   # nginx's redirects
	compose = _compose_files()
	if TRIAL_ENTRY in compose:
		manage.env_set({"COMPOSE_FILE": ",".join(f for f in compose if f != TRIAL_ENTRY)})
	trial_path().unlink(missing_ok=True)
	state = {"keep": "applied", "rollback": "rolled_back"}.get(outcome, "failed")
	return write_status(id_, state, current_port(), message=message, now=now)
