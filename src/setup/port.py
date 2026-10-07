"""The port helper's thinking (the HTTPS port changed in System Settings):
the scripts do what needs Docker, this decides and records. The contract
with the app is in docs/plans/stage-9.md and src/access/port.py:

  site.env (src/access/site_env.py)  app -> helper: the port wanted, a request id,
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

The trial's 120 s are timed by the script that runs it (its own stopwatch:
Docker Desktop's VM clock can fall minutes behind Windows' under load, so
comparing the two misjudges the deadline) - it closes with --timed-out.
The deadline recorded here (the VM's clock) is for the page's countdown, the
app's confirm check, and a trial whose helper died (then `next_step` rolls
it back).
"""
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src import runtime
from src.access import site_env
from src.setup.env import compose_files, env_read, env_set

STATUS_FILE = "apply-status.json"          # src/access/port.py reads it
TRIAL_FILE = "port-trial.yaml"             # in config/, listed in COMPOSE_FILE
TRIAL_ENTRY = "config/" + TRIAL_FILE       # relative to the install folder
TRIAL_SECONDS = 120                        # time to click through a certificate warning


@dataclass
class Step:
	"""What the port helper does next (port-next prints it)."""
	action: str               # none / wait / try / keep / rollback
	port: int | None = None   # try: the new port
	id: str = ""
	message: str = ""         # what to say (refused, rolled back: why)


def status_path() -> Path:
	return runtime.config_dir() / STATUS_FILE


def trial_path() -> Path:
	return runtime.config_dir() / TRIAL_FILE


def read_status() -> dict[str, Any] | None:
	return runtime.read_json(status_path())


def write_status(id_: str, state: str, port: int, trying: int | None = None,
                 deadline: float | None = None, message: str = "",
                 now: float | None = None) -> dict[str, Any]:
	"""Write apply-status.json - what the page shows and waits on.

	:param id_: the request it's about
	:param state: e.g. waiting / trying / applied / rolled-back / failed
	:param port: the port in use
	:param trying: the trial's new port
	:param deadline: when the trial ends (epoch seconds)
	:param message: why (a rollback, a failure)
	:param now: the time it's stamped with (tests); now when None
	:returns: what was written"""
	now = now if now is not None else datetime.datetime.now().timestamp()
	status = {"id": id_, "state": state, "port": port, "trying": trying,
	          "deadline": deadline, "message": message,
	          "time": datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds")}
	runtime.write_json(status_path(), status)
	return status


def ready() -> bool:
	"""A helper is here (the Windows helper, Linux's systemd unit): the page
	learns it before the first change - without a status it says to run
	`netrollout apply` by hand. Only when there's none yet; True if written."""
	if status_path().exists():
		return False
	write_status("", "applied", current_port())
	return True


def current_port() -> int:
	"""The port Docker publishes now (.env's HTTPS_PORT)."""
	try:
		return int(env_read().get("HTTPS_PORT", "443"))
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
			return Step("rollback", id=req_id, message=timed_out(status.get("trying")))
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


def timed_out(trial_port: int | None) -> str:
	""":returns: why a trial was rolled back when its time ran out"""
	return (f"not confirmed within {TRIAL_SECONDS} s - port {trial_port} didn't "
	        f"open from a browser (a firewall?)")


def open_trial(port: int) -> None:
	"""The trial's compose file (port P next to the current one) and its
	place in COMPOSE_FILE - every compose call keeps it during the trial."""
	trial_path().parent.mkdir(parents=True, exist_ok=True)
	trial_path().write_text(
		"# NetRollout's port helper: a trial of a new HTTPS port, next to the\n"
		"# current one - removed when it's kept or rolled back\n"
		"services:\n  nginx:\n    ports:\n"
		f'      - "{port}:443"\n', encoding="utf-8")
	compose = compose_files(env_read())
	if TRIAL_ENTRY not in compose:
		env_set({"COMPOSE_FILE": ",".join(compose + [TRIAL_ENTRY])})


def trying(port: int, id_: str, now: float | None = None) -> dict:
	"""nginx answers on both ports: the trial's clock starts."""
	now = now if now is not None else datetime.datetime.now().timestamp()
	return write_status(id_, "trying", current_port(), trying=port,
	                    deadline=now + TRIAL_SECONDS, now=now)


def close(outcome: str, id_: str, message: str = "",
          now: float | None = None, timed_out_: bool = False) -> dict:
	"""The trial ends: keep (the new port in .env and site.env), rollback or
	failed (the current port stays). The trial file goes either way; then
	the script recreates nginx. timed_out_: the script's stopwatch ran out
	(the message says so)."""
	status = read_status() or {}
	new = status.get("trying")
	if timed_out_:
		message = timed_out(new)
	if outcome == "keep" and new:
		env_set({"HTTPS_PORT": str(new)})
		site_env.update({site_env.HTTPS_PORT: str(new)})   # nginx's redirects
	compose = compose_files(env_read())
	if TRIAL_ENTRY in compose:
		env_set({"COMPOSE_FILE": ",".join(f for f in compose if f != TRIAL_ENTRY)})
	trial_path().unlink(missing_ok=True)
	state = {"keep": "applied", "rollback": "rolled_back"}.get(outcome, "failed")
	return write_status(id_, state, current_port(), message=message, now=now)
