"""A database move, run in the background (stage 9.8): Server Management's
Move to another server / Move back to the bundled database.

  1. waiting - new rollouts paused (maintenance), the queued and running ones
     finish; the admin may cancel rollouts, or the move. Not done within
     WAIT_SECONDS -> the move is given up and rollouts resume;
  2. locked - the maintenance page for everyone; src/db/move.copy: a
     before-move backup, restored into the target, row counts compared;
  3. the switch - the app's connection replaced live and kept in
     config/runtime.env; maintenance ends.
Anything failing before 3 leaves NetRollout on its database, unchanged. One
move at a time (maintenance's own lock). The outcome stays on the page
until the next move; audited (database.moved in the target, written by the
copy; database.move_failed here).

Maintenance mode, the move's first two steps: the data is copied from a
snapshot and the app then switches to the copy, so nothing may write in
between - a row written after the snapshot would be lost at the switch.
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
import threading
import time
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, TYPE_CHECKING, TypeVar

from flask import Response, g, render_template, request
from flask.typing import ResponseReturnValue

from src.audit import Actor, AuditAction
from src.db import move
from src.db.connections import PostgresConfig
from src.webapp.app import NetRolloutApp, current_app
from src.webapp.http import Caller, err
if TYPE_CHECKING:   # annotations only: the orchestrator loads the database stack
	from src.jobs import RolloutOrchestrator


WAIT_SECONDS = 30 * 60
POLL_SECONDS = 1.0
class MoveState(StrEnum):
	"""Where a database move is (DatabaseMove.status()'s state, the page's)."""
	IDLE = "idle"              # none yet
	WAITING = "waiting"        # for the rollouts (maintenance: waiting)
	COPYING = "copying"        # maintenance: locked
	SWITCHING = "switching"
	DONE = "done"
	FAILED = "failed"
	CANCELLED = "cancelled"


class MaintenanceState(StrEnum):
	"""This process's maintenance (Maintenance.state): idle -> waiting ->
	locked -> idle."""
	IDLE = "idle"
	WAITING = "waiting"        # new rollouts paused, the rest goes on
	LOCKED = "locked"          # nothing may write


def same_database(a: PostgresConfig, b: PostgresConfig) -> bool:
	""":returns: whether both name the same place (host, port, database,
	 schema)"""
	return a.place() == b.place()


class DatabaseMove:
	"""This process's database move (app.db_move): at most one at a time."""

	def __init__(self, app: NetRolloutApp, wait_seconds: float = WAIT_SECONDS,
	             poll_seconds: float = POLL_SECONDS) -> None:
		""":param wait_seconds: how long to wait for rollouts before giving up
		:param poll_seconds: how often to look whether they're done"""
		self._app = app
		self._wait = wait_seconds
		self._poll = poll_seconds
		self._cancel = threading.Event()
		self._status: dict[str, Any] = {"state": MoveState.IDLE}

	def status(self) -> dict[str, Any]:
		"""For the page: state, step, target, times, outcome."""
		return dict(self._status)

	def seconds_left(self) -> float:
		"""Until the wait for rollouts gives up."""
		return self._status.get("deadline", 0) - time.time()

	@property
	def running(self) -> bool:
		""":returns: whether a move is under way (not idle nor ended)"""
		return self._status["state"] in (MoveState.WAITING, MoveState.COPYING, MoveState.SWITCHING)

	def start(self, target: PostgresConfig, actor_id: uuid.UUID | None, actor: str,
	          back: bool = False, replace: bool = False) -> None:
		"""Checks the target, then moves in a thread - audited as
		database.move_started first: once the thread runs, maintenance may lock
		at once, and a row written then would be lost (the row is copied with
		the database).

		:param actor_id: the admin moving it (and actor, their username)
		:param back: a move back to the bundled database (the wording)
		:param replace: a NetRollout database there may be overwritten (the
		 page's "replace" box; a move back always replaces)
		:raises move.MoveError: refused (the reason in words)"""
		backend = self._app.backend
		if same_database(target, backend.postgres.config):
			raise move.MoveError("That is the database NetRollout uses now.")
		report = move.check_target(target)
		if not report.ok:
			raise move.MoveError(" ".join(report.problems))
		if report.contents == move.NETROLLOUT and not replace:
			raise move.MoveError("It holds a NetRollout database - tick 'replace' to "
			                     "overwrite it.")
		what = ("moving back to the bundled database" if back
		        else "moving to another database")
		if not self._app.maintenance.begin(what, actor_id):
			raise move.MoveError("A move is under way already.")
		self._cancel.clear()
		self._status = {"state": MoveState.WAITING, "step": "Waiting for rollouts to finish",
		                "what": what, "back": back,
		                "source": backend.postgres.describe(),
		                "target": target.describe(), "actor": actor,
		                "started": _now(), "deadline": time.time() + self._wait}
		try:   # never stops the move (maintenance has begun): a failure is printed
			self._app.web.audit_trail.record(
				Actor(actor_id, actor), AuditAction.DATABASE_MOVE_STARTED,
				object_type="database", object_label=target.describe(), detail={"back": back})
		except Exception as e:                    # noqa: BLE001
			print(f"[NetRollout] database move: start not audited ({e})", flush=True)
		threading.Thread(target=self._run, args=(target, actor_id, actor),
		                 name="database-move", daemon=True).start()

	def cancel(self) -> bool:
		"""Gives the move up - only while it waits for rollouts.

		:returns: False: too late (or nothing to cancel)"""
		if self._status["state"] != MoveState.WAITING:
			return False
		self._cancel.set()
		return True

	def _step(self, state: MoveState, step: str) -> None:
		"""The step under way: the status, maintenance's progress, the console."""
		self._status.update(state=state, step=step)
		self._app.maintenance.report(step)
		print(f"[NetRollout] database move: {step}", flush=True)

	def _run(self, target: PostgresConfig, actor_id: uuid.UUID | None,
	         actor: str) -> None:
		"""The move itself (its thread): wait for the lock, copy, switch.
		Maintenance ends whatever happens; the outcome stays in the status."""
		maintenance = self._app.maintenance
		outcome, message = MoveState.FAILED, ""
		try:
			while not maintenance.lock():
				if self._cancel.is_set():
					outcome, message = MoveState.CANCELLED, "Cancelled - NetRollout stays on its database."
					return
				if time.time() >= self._status["deadline"]:
					message = (f"Rollouts were still running after {int(self._wait // 60)} "
					           f"minutes - the move was given up and rollouts resumed. "
					           f"Cancel the stuck rollouts, then move again.")
					return
				time.sleep(self._poll)
			self._step(MoveState.COPYING, "Copying the data")
			detail: dict[str, Any] = {"from": self._status["source"], "to": self._status["target"],
			          "by": actor}
			copied = move.copy(self._app.backend.postgres.engine, target, detail=detail,
			                   report=lambda step: self._step(MoveState.COPYING, step))
			self._status["backup"] = copied.backup.name
			self._step(MoveState.SWITCHING, "Switching to the new database")
			try:
				self._app.backend.move_postgres(target)
			except RuntimeError as e:
				raise move.MoveError(f"Switching failed: {e} - NetRollout stays on its "
				                     f"database.") from None
			outcome = MoveState.DONE
			message = f"NetRollout now uses {self._status['target']}."
		except move.MoveError as e:
			message = str(e)
		except Exception as e:                    # noqa: BLE001 - shown, not lost
			message = f"The move failed: {e} - NetRollout stays on its database."
		finally:
			maintenance.end()
			self._status.update(state=outcome, step="", message=message, finished=_now())
			print(f"[NetRollout] database move: {outcome} - {message}", flush=True)
			if outcome != MoveState.DONE:
				self._audit_failure(outcome, message, actor_id, actor)

	def _audit_failure(self, outcome: MoveState, message: str,
	                   actor_id: uuid.UUID | None, actor: str) -> None:
		"""A move that didn't happen, in the audit log of the database
		NetRollout stays on (never stops: a failure to audit is printed)."""
		try:
			self._app.web.audit_trail.record(
				Actor(actor_id, actor),
				AuditAction.DATABASE_MOVE_CANCELLED if outcome == MoveState.CANCELLED
				else AuditAction.DATABASE_MOVE_FAILED, object_type="database",
				object_label=self._status.get("target"), success=False,
				detail={"from": self._status.get("source"), "message": message})
		except Exception as e:                    # noqa: BLE001
			print(f"[NetRollout] database move: not audited ({e})", flush=True)


def _now() -> str:
	""":returns: the local time, ISO 8601 to the second"""
	return datetime.now().isoformat(timespec="seconds")


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
