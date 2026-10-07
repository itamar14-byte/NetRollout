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
copy; database.move_failed here)."""
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import make_url

from src.db import move
from src.db.postgres_db import PostgresConfig
from src.db.tables import AuditLog
from src.webapp.flask_app import NetRolloutApp

WAIT_SECONDS = 30 * 60
POLL_SECONDS = 1.0
IDLE, WAITING, COPYING, SWITCHING, DONE, FAILED, CANCELLED = (
	"idle", "waiting", "copying", "switching", "done", "failed", "cancelled")


def describe(config: PostgresConfig) -> str:
	"""host:port/database[/schema], no password - for pages and the audit."""
	url = make_url(config.get_url())
	place = f"{url.host}:{url.port or 5432}/{url.database}"
	return place + (f" (schema {config.schema})" if config.schema else "")


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
		self._status: dict[str, Any] = {"state": IDLE}

	def status(self) -> dict[str, Any]:
		"""For the page: state, step, target, times, outcome."""
		return dict(self._status)

	def seconds_left(self) -> float:
		"""Until the wait for rollouts gives up."""
		return self._status.get("deadline", 0) - time.time()

	@property
	def running(self) -> bool:
		""":returns: whether a move is under way (not idle nor ended)"""
		return self._status["state"] in (WAITING, COPYING, SWITCHING)

	def start(self, target: PostgresConfig, actor_id: uuid.UUID | None, actor: str,
	          back: bool = False) -> None:
		"""Checks the target, then moves in a thread.

		:param actor_id: the admin moving it (and actor, their username)
		:param back: a move back to the bundled database (the wording)
		:raises move.MoveError: refused (the reason in words)"""
		backend = self._app.backend
		if same_database(target, backend.postgres.config):
			raise move.MoveError("That is the database NetRollout uses now.")
		report = move.check_target(target)
		if not report.ok:
			raise move.MoveError(" ".join(report.problems))
		what = ("moving back to the bundled database" if back
		        else "moving to another database")
		if not self._app.maintenance.begin(what, actor_id):
			raise move.MoveError("A move is under way already.")
		self._cancel.clear()
		self._status = {"state": WAITING, "step": "Waiting for rollouts to finish",
		                "what": what, "back": back,
		                "source": describe(backend.postgres.config),
		                "target": describe(target), "actor": actor,
		                "started": _now(), "deadline": time.time() + self._wait}
		threading.Thread(target=self._run, args=(target, actor_id, actor),
		                 name="database-move", daemon=True).start()

	def cancel(self) -> bool:
		"""Gives the move up - only while it waits for rollouts.

		:returns: False: too late (or nothing to cancel)"""
		if self._status["state"] != WAITING:
			return False
		self._cancel.set()
		return True

	def _step(self, state: str, step: str) -> None:
		"""The step under way: the status, maintenance's progress, the console."""
		self._status.update(state=state, step=step)
		self._app.maintenance.report(step)
		print(f"[NetRollout] database move: {step}", flush=True)

	def _run(self, target: PostgresConfig, actor_id: uuid.UUID | None,
	         actor: str) -> None:
		"""The move itself (its thread): wait for the lock, copy, switch.
		Maintenance ends whatever happens; the outcome stays in the status."""
		maintenance = self._app.maintenance
		outcome, message = FAILED, ""
		try:
			while not maintenance.lock():
				if self._cancel.is_set():
					outcome, message = CANCELLED, "Cancelled - NetRollout stays on its database."
					return
				if time.time() >= self._status["deadline"]:
					message = (f"Rollouts were still running after {int(self._wait // 60)} "
					           f"minutes - the move was given up and rollouts resumed. "
					           f"Cancel the stuck rollouts, then move again.")
					return
				time.sleep(self._poll)
			self._step(COPYING, "Copying the data")
			detail: dict[str, Any] = {"from": self._status["source"], "to": self._status["target"],
			          "by": actor}
			copied = move.copy(self._app.backend.postgres.engine, target, detail=detail,
			                   report=lambda step: self._step(COPYING, step))
			self._status["backup"] = copied.backup.name
			self._step(SWITCHING, "Switching to the new database")
			try:
				self._app.backend.move_postgres(target)
			except RuntimeError as e:
				raise move.MoveError(f"Switching failed: {e} - NetRollout stays on its "
				                     f"database.") from None
			outcome = DONE
			message = f"NetRollout now uses {self._status['target']}."
		except move.MoveError as e:
			message = str(e)
		except Exception as e:                    # noqa: BLE001 - shown, not lost
			message = f"The move failed: {e} - NetRollout stays on its database."
		finally:
			maintenance.end()
			self._status.update(state=outcome, step="", message=message, finished=_now())
			print(f"[NetRollout] database move: {outcome} - {message}", flush=True)
			if outcome != DONE:
				self._audit_failure(outcome, message, actor_id, actor)

	def _audit_failure(self, outcome: str, message: str,
	                   actor_id: uuid.UUID | None, actor: str) -> None:
		"""A move that didn't happen, in the audit log of the database
		NetRollout stays on (never stops: a failure to audit is printed)."""
		try:
			with self._app.backend.postgres.get_session() as session:
				session.add(AuditLog(
					actor_id=actor_id, actor_username=actor,
					action="database.move_cancelled" if outcome == CANCELLED
					else "database.move_failed", object_type="database",
					object_label=self._status.get("target"), success=False,
					detail={"from": self._status.get("source"), "message": message}))
		except Exception as e:                    # noqa: BLE001
			print(f"[NetRollout] database move: not audited ({e})", flush=True)


def _now() -> str:
	""":returns: the local time, ISO 8601 to the second"""
	return datetime.now().isoformat(timespec="seconds")
