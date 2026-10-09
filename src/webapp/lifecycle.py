"""This process's states beside serving: a stop or restart (Shutdown) and
maintenance (Maintenance).

Shutdown: the one way this process stops on purpose — a SIGTERM (docker
stop, netrollout stop / update) or the admin Restart: drain the
orchestrator, then exit. In a container the restart policy (unless-stopped)
brings a Restart back; in development the process relaunches itself.

Maintenance, a database move's first two steps (src/webapp/db_move.py): the
data is copied from a snapshot and the app then switches to the copy, so
nothing may write in between - a row written after the snapshot would be
lost at the switch.
- **waiting**: new rollouts are refused (the orchestrator's pause), the queued
  and running ones finish (or are cancelled by the admin); the site works,
  with a banner;
- **locked** once nothing runs any more - no rollout, and no request being
  answered that may write (every one but those below and the live log's
  stream is counted while it runs); every request but the few marked
  `@during_maintenance` (the move's progress, health, Grafana's read-only
  auth check, ...) gets the maintenance page (503), the moving admin's
  included; audit rows and the background jobs that write wait.
The state lives in this process only, never in Redis (which isn't moved and
outlives a restart): a restart mid-move ends maintenance, and that is safe
because the switch is the move's last step."""
import os
import subprocess
import sys
import threading
import time
import uuid
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeVar

from flask import Response, g, render_template, request
from flask.typing import ResponseReturnValue

from src.runtime import in_container
from src.webapp.app import NetRolloutApp, current_app
from src.webapp.http import Caller, err
from src.webapp.startup import RELAUNCH_ENV

if TYPE_CHECKING:   # annotations only: the orchestrator loads the database stack
	from src.jobs import RolloutOrchestrator

# Lets the HTTP response to the Restart request go out before the exit
_EXIT_DELAY = 1.5


def relaunch_command() -> list[str]:
	"""The command that started this process, to start it again (development).
	Not sys.argv: under `python -m src.webapp` its [0] is the __main__.py
	path - run as a script, `src` isn't importable and the restarted app
	never comes back. orig_argv keeps `-m src.webapp`."""
	return [sys.executable, *sys.orig_argv[1:]]


class Shutdown:
	"""This process's stop or restart (app.shutdown): at most one, begun once."""

	def __init__(self, orchestrator: "RolloutOrchestrator") -> None:
		self._orchestrator = orchestrator
		self._lock = threading.Lock()
		self._restart: bool | None = None   # None: not begun

	@property
	def in_progress(self) -> bool:
		""":returns: whether a stop or restart has begun"""
		return self._restart is not None

	@property
	def restarting(self) -> bool:
		""":returns: whether what has begun is a restart"""
		return bool(self._restart)

	def begin(self, deadline: float, restart: bool) -> bool:
		"""Drain (up to `deadline` seconds for running rollouts), then exit —
		in the background, so the app keeps serving pages (and the banner)
		meanwhile.

		:param restart: start again afterwards (else stop)
		:returns: False if a stop is already in progress"""
		with self._lock:
			if self._restart is not None:
				return False
			self._restart = restart
		print(f"[NetRollout] {'Restart' if restart else 'Stop'} requested — "
		      f"new rollouts are paused", flush=True)
		threading.Thread(target=self._run, args=(deadline, restart),
		                 name="shutdown", daemon=True).start()
		return True

	def _run(self, deadline: float, restart: bool) -> None:
		"""The drain, then the exit (relaunched first in development) - the
		exit even if the drain failed."""
		try:
			self._orchestrator.drain(
				deadline, report=lambda line: print(line, flush=True))
		finally:
			if restart:   # a stop has no response waiting to go out
				time.sleep(_EXIT_DELAY)
			if restart and not in_container():
				# marker: the relaunched app doesn't open another browser tab
				subprocess.Popen(relaunch_command(),
				                 env={**os.environ, RELAUNCH_ENV: "1"})
			print("[NetRollout] Stopped", flush=True)
			os._exit(0)


class MaintenanceState(StrEnum):
	"""This process's maintenance (Maintenance.state): idle -> waiting ->
	locked -> idle."""
	IDLE = "idle"
	WAITING = "waiting"        # new rollouts paused, the rest goes on
	LOCKED = "locked"          # nothing may write


RETRY_AFTER_SECONDS = 10
# not views of ours: the files the pages need, Prometheus' scrape (Redis only)
ALWAYS_SERVED = {"static", "prometheus_metrics"}
# refused while locked, but not waited for by the lock: the live log's stream
# is open for a whole rollout (the lock waits for the rollout) and doesn't write
NOT_WAITED_FOR = {"rollout.rollout_stream"}
V = TypeVar("V")


def during_maintenance(view: V) -> V:
	"""Marks a view as still served while locked - it must not write to the
	database (the move's progress, health, ...)."""
	setattr(view, "during_maintenance", True)
	return view


class Maintenance:
	"""This process's maintenance state (app.maintenance): idle → waiting →
	locked → idle."""

	def __init__(self, orchestrator: "RolloutOrchestrator") -> None:
		self._orchestrator = orchestrator
		self._lock = threading.Lock()
		self._state = MaintenanceState.IDLE
		self._what = ""
		self._progress = ""
		self._actor_id: uuid.UUID | None = None
		self._under_way = 0       # requests that may write, being answered

	@property
	def state(self) -> MaintenanceState:
		""":returns: idle, waiting or locked"""
		return self._state

	@property
	def active(self) -> bool:
		""":returns: whether maintenance is under way (waiting or locked)"""
		return self._state != MaintenanceState.IDLE

	@property
	def writes_blocked(self) -> bool:
		""":returns: whether nothing may write (locked)"""
		return self._state == MaintenanceState.LOCKED

	def begin(self, what: str, actor_id: uuid.UUID | None) -> bool:
		"""Waiting: new rollouts paused.

		:param what: what's under way, for the banner and the page
		:param actor_id: the admin who started it
		:returns: False when something is under way already (one at a time)"""
		with self._lock:
			if self._state != MaintenanceState.IDLE:
				return False
			self._state, self._what, self._actor_id = MaintenanceState.WAITING, what, actor_id
			self._progress = "Waiting for rollouts to finish"
			self._orchestrator.pause()
			return True

	def lock(self) -> bool:
		"""Locked, once no rollout is queued or running and no request that
		may write is being answered; else False (still waiting)."""
		with self._lock:
			if self._state != MaintenanceState.WAITING:
				return False
			if self._under_way or not self._orchestrator.idle():
				return False
			self._state = MaintenanceState.LOCKED
			return True

	def enter(self) -> bool:
		"""A request that may write arrives (the gate): counted while it is
		answered - lock() waits for it - unless locked.

		:returns: False: locked - refused, not counted"""
		with self._lock:
			if self._state == MaintenanceState.LOCKED:
				return False
			self._under_way += 1
			return True

	def leave(self) -> None:
		"""A request enter() counted has ended (answered or failed)."""
		with self._lock:
			self._under_way -= 1

	def report(self, progress: str) -> None:
		"""The step under way, for the pages (the move's)."""
		self._progress = progress

	def end(self) -> None:
		"""Back to normal (moved, failed or cancelled): rollouts resume."""
		with self._lock:
			self._state, self._what, self._progress, self._actor_id = MaintenanceState.IDLE, "", "", None
			self._orchestrator.resume()

	def snapshot(self) -> dict[str, Any]:
		""":returns: {state, what, progress, actor_id} for the pages"""
		return {"state": self._state, "what": self._what,
		        "progress": self._progress, "actor_id": self._actor_id}


def register_maintenance(app: NetRolloutApp) -> None:
	"""app.maintenance, its gate (registered before every other request hook,
	so session and sign-in hooks can't write while locked) and the banner."""
	app.maintenance = Maintenance(app.orchestrator)

	@app.before_request
	def maintenance_gate() -> Response | None:
		"""While locked: 503 + Retry-After (JSON for a request from a page,
		else the maintenance page) for every view not marked
		@during_maintenance. Otherwise each request that may write is counted
		while it is answered (Maintenance.enter): the lock waits for it.

		:returns: None: go on; else the answer"""
		maintenance = current_app.maintenance
		# no endpoint (a 404): no view, so the maintenance page
		view = current_app.view_functions.get(request.endpoint or "")
		if request.endpoint in ALWAYS_SERVED or getattr(view, "during_maintenance", False):
			return None
		if request.endpoint in NOT_WAITED_FOR:
			if not maintenance.writes_blocked:
				return None
		elif maintenance.enter():
			g.maintenance_counted = True
			return None
		info = maintenance.snapshot()
		message = f"NetRollout is under maintenance - {info['what']}. Try again in a few minutes."
		# by the same rule as a session that ended
		if Caller.SESSION_CHECK.wants_json():
			answer: ResponseReturnValue = err(message, 503, maintenance=True)
		else:
			answer = render_template("maintenance.html", info=info), 503
		response = current_app.make_response(answer)
		response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
		return response

	@app.teardown_request
	def maintenance_request_ended(_error: BaseException | None) -> None:
		"""A request the gate counted has ended - answered or failed."""
		if g.pop("maintenance_counted", False):
			current_app.maintenance.leave()

	@app.context_processor
	def maintenance_banner() -> dict[str, Any]:
		"""The banner's details while waiting (locked: the pages aren't served)."""
		info = app.maintenance.snapshot()
		return {"maintenance": info} if info["state"] == MaintenanceState.WAITING else {}
