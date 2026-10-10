"""Moving NetRollout's services (Server Management): the database move, run
in the background (stage 9.8), and the live Redis switch.

The database move - Move to another server / Move back to the bundled
database:

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

Maintenance mode (src/webapp/lifecycle.py) holds the writes from the
waiting to the switch.

The Redis switch (switch_redis) copies nothing: Redis holds sessions and
live rollout state only."""
import dataclasses
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from src.accounts.users import SessionStore
from src.audit import Actor, AuditAction, AuditTrail
from src.db import move
from src.db.connections import BackendServices, PostgresConfig, RedisConfig
from src.jobs import RolloutOrchestrator, clear_stale_jobs
from src.webapp.lifecycle import Maintenance


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


@dataclass
class MoveStatus:
	"""A database move's progress and outcome (DatabaseMove.status()): a
	field is in the page's JSON once set."""
	state: MoveState = MoveState.IDLE
	step: str | None = None          # the step under way ("" once ended)
	what: str | None = None          # the move, in words
	back: bool | None = None         # a move back to the bundled database
	source: str | None = None        # where from / to (describe())
	target: str | None = None
	actor: str | None = None         # the admin's username
	started: str | None = None       # local time, ISO 8601
	deadline: float | None = None    # the wait for rollouts gives up (epoch seconds)
	backup: str | None = None        # the before-move backup's file name
	message: str | None = None       # the outcome, in words
	finished: str | None = None

	def as_dict(self) -> dict[str, Any]:
		""":returns: every field set"""
		return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


def same_database(a: PostgresConfig, b: PostgresConfig) -> bool:
	""":returns: whether both name the same place (host, port, database,
	 schema)"""
	return a.place() == b.place()


class DatabaseMove:
	"""This process's database move (app.db_move): at most one at a time."""

	def __init__(self, backend: BackendServices, maintenance: Maintenance,
	             audit_trail: AuditTrail, wait_seconds: float = WAIT_SECONDS,
	             poll_seconds: float = POLL_SECONDS) -> None:
		""":param backend: the connections (the move switches Postgres)
		:param maintenance: the pause and the lock around the copy
		:param audit_trail: where the start and a failure are recorded
		:param wait_seconds: how long to wait for rollouts before giving up
		:param poll_seconds: how often to look whether they're done"""
		self._backend = backend
		self._maintenance = maintenance
		self._audit_trail = audit_trail
		self._wait = wait_seconds
		self._poll = poll_seconds
		self._cancel = threading.Event()
		self._status = MoveStatus()

	def status(self) -> dict[str, Any]:
		"""For the page: state, step, target, times, outcome (MoveStatus)."""
		return self._status.as_dict()

	def seconds_left(self) -> float:
		"""Until the wait for rollouts gives up."""
		return (self._status.deadline or 0) - time.time()

	@property
	def running(self) -> bool:
		""":returns: whether a move is under way (not idle nor ended)"""
		return self._status.state in (MoveState.WAITING, MoveState.COPYING, MoveState.SWITCHING)

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
		backend = self._backend
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
		if not self._maintenance.begin(what, actor_id):
			raise move.MoveError("A move is under way already.")
		self._cancel.clear()
		self._status = MoveStatus(MoveState.WAITING, step="Waiting for rollouts to finish",
		                          what=what, back=back,
		                          source=backend.postgres.describe(),
		                          target=target.describe(), actor=actor,
		                          started=_now(), deadline=time.time() + self._wait)
		try:   # never stops the move (maintenance has begun): a failure is printed
			self._audit_trail.record(
				Actor(actor_id, actor), AuditAction.DATABASE_MOVE_STARTED,
				object_type="database", object_label=target.describe(), detail={"back": back})
		except Exception as e:                    # noqa: BLE001
			print(f"[NetRollout] database move: start not audited ({e})", flush=True)
		threading.Thread(target=self._run, args=(target, actor_id, actor),
		                 name="database-move", daemon=True).start()

	def cancel(self) -> bool:
		"""Gives the move up - only while it waits for rollouts.

		:returns: False: too late (or nothing to cancel)"""
		if self._status.state != MoveState.WAITING:
			return False
		self._cancel.set()
		return True

	def _step(self, state: MoveState, step: str) -> None:
		"""The step under way: the status, maintenance's progress, the console."""
		self._status.state, self._status.step = state, step
		self._maintenance.report(step)
		print(f"[NetRollout] database move: {step}", flush=True)

	def _run(self, target: PostgresConfig, actor_id: uuid.UUID | None,
	         actor: str) -> None:
		"""The move itself (its thread): wait for the lock, copy, switch.
		Maintenance ends whatever happens; the outcome stays in the status."""
		maintenance = self._maintenance
		outcome, message = MoveState.FAILED, ""
		try:
			while not maintenance.lock():
				if self._cancel.is_set():
					outcome, message = MoveState.CANCELLED, "Cancelled - NetRollout stays on its database."
					return
				if time.time() >= (self._status.deadline or 0):
					message = (f"Rollouts were still running after {int(self._wait // 60)} "
					           f"minutes - the move was given up and rollouts resumed. "
					           f"Cancel the stuck rollouts, then move again.")
					return
				time.sleep(self._poll)
			self._step(MoveState.COPYING, "Copying the data")
			detail: dict[str, Any] = {"from": self._status.source, "to": self._status.target,
			                          "by": actor}
			copied = move.copy(self._backend.postgres.engine, target, detail=detail,
			                   report=lambda step: self._step(MoveState.COPYING, step))
			self._status.backup = copied.backup.name
			self._step(MoveState.SWITCHING, "Switching to the new database")
			try:
				self._backend.move_postgres(target)
			except RuntimeError as e:
				raise move.MoveError(f"Switching failed: {e} - NetRollout stays on its "
				                     f"database.") from None
			outcome = MoveState.DONE
			message = f"NetRollout now uses {self._status.target}."
		except move.MoveError as e:
			message = str(e)
		except Exception as e:                    # noqa: BLE001 - shown, not lost
			message = f"The move failed: {e} - NetRollout stays on its database."
		finally:
			maintenance.end()
			# audited before the move is shown as over: whoever sees it failed
			# (the page, a test) finds its audit row
			if outcome != MoveState.DONE:
				self._audit_failure(outcome, message, actor_id, actor)
			self._status.state, self._status.step = outcome, ""
			self._status.message, self._status.finished = message, _now()
			print(f"[NetRollout] database move: {outcome} - {message}", flush=True)

	def _audit_failure(self, outcome: MoveState, message: str,
	                   actor_id: uuid.UUID | None, actor: str) -> None:
		"""A move that didn't happen, in the audit log of the database
		NetRollout stays on (never stops: a failure to audit is printed)."""
		try:
			self._audit_trail.record(
				Actor(actor_id, actor),
				AuditAction.DATABASE_MOVE_CANCELLED if outcome == MoveState.CANCELLED
				else AuditAction.DATABASE_MOVE_FAILED, object_type="database",
				object_label=self._status.target, success=False,
				detail={"from": self._status.source, "message": message})
		except Exception as e:                    # noqa: BLE001
			print(f"[NetRollout] database move: not audited ({e})", flush=True)


def _now() -> str:
	""":returns: the local time, ISO 8601 to the second"""
	return datetime.now().isoformat(timespec="seconds")


# ── Redis: a live switch ─────────────────────────────────────────────────────

class RolloutsRunning(Exception):
	"""A Redis switch refused: rollouts run, and their live state is in Redis."""


def switch_redis(backend: BackendServices, orchestrator: RolloutOrchestrator,
                 sessions: SessionStore, config: RedisConfig) -> None:
	"""Switch to the Redis `config` names - live, no restart: everything looks
	the client up per use. Refused while rollouts run (their live state is in
	Redis). Sessions and leftover job state are cleared in the Redis switched
	to - one used before still holds old sessions, terminated ones included -
	so everyone signs in again (the admin who switches stays signed in: their
	request saves its session into the new Redis at its end). New rollouts are
	paused from the check to the switch, so none starts in between (a pause
	already on - a database move's - is left on).

	:raises RolloutsRunning: rollouts are queued or running - nothing changed
	:raises RuntimeError: the new server doesn't answer - nothing changed"""
	was_paused = orchestrator.paused
	orchestrator.pause()
	try:
		if any(orchestrator.counts().values()):
			raise RolloutsRunning("Rollouts are running - their live state is in Redis. "
			                      "Switch once they've finished.")
		backend.reload_redis(config)
		sessions.clear_all()
		clear_stale_jobs(backend.redis)   # before a new rollout's keys go there
	finally:
		if not was_paused:
			orchestrator.resume()
