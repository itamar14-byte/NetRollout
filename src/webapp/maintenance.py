"""Maintenance mode, for a database move (stage 9.8): the data is copied from
a snapshot and the app then switches to the copy, so nothing may write in
between - a row written after the snapshot would be lost at the switch.

Two phases:
- **waiting**: new rollouts are refused (the orchestrator's pause), the queued
  and running ones finish (or are cancelled by the admin); the site works,
  with a banner;
- **locked**: nothing runs any more; every request but the few marked
  `@during_maintenance` (the move's progress, health, Grafana's read-only
  auth check, ...) gets the maintenance page (503), the moving admin's
  included; audit rows and the background jobs that write wait.

The state lives in this process only, never in Redis (which isn't moved and
outlives a restart): a restart mid-move ends maintenance, and that is safe
because the switch is the move's last step."""
import threading
import uuid
from typing import TYPE_CHECKING, Any, TypeVar

from flask import Response, render_template, request
from flask.typing import ResponseReturnValue

from src.webapp.flask_app import NetRolloutApp, current_app
from src.webapp.utils import err

if TYPE_CHECKING:   # annotations only: the orchestrator loads the database stack
	from src.orchestration import RolloutOrchestrator

IDLE, WAITING, LOCKED = "idle", "waiting", "locked"
RETRY_AFTER_SECONDS = 10
# not views of ours: the files the pages need, Prometheus' scrape (Redis only)
ALWAYS_SERVED = {"static", "prometheus_metrics"}
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
		self._state = IDLE
		self._what = ""
		self._progress = ""
		self._actor_id: uuid.UUID | None = None

	@property
	def state(self) -> str:
		""":returns: IDLE, WAITING or LOCKED"""
		return self._state

	@property
	def writes_blocked(self) -> bool:
		""":returns: whether nothing may write (locked)"""
		return self._state == LOCKED

	def begin(self, what: str, actor_id: uuid.UUID | None) -> bool:
		"""Waiting: new rollouts paused.

		:param what: what's under way, for the banner and the page
		:param actor_id: the admin who started it
		:returns: False when something is under way already (one at a time)"""
		with self._lock:
			if self._state != IDLE:
				return False
			self._state, self._what, self._actor_id = WAITING, what, actor_id
			self._progress = "Waiting for rollouts to finish"
			self._orchestrator.pause()
			return True

	def lock(self) -> bool:
		"""Locked, once no rollout is queued or running; else False (still
		waiting)."""
		with self._lock:
			if self._state != WAITING:
				return False
			if not self._orchestrator.idle():
				return False
			self._state = LOCKED
			return True

	def report(self, progress: str) -> None:
		"""The step under way, for the pages (the move's)."""
		self._progress = progress

	def end(self) -> None:
		"""Back to normal (moved, failed or cancelled): rollouts resume."""
		with self._lock:
			self._state, self._what, self._progress, self._actor_id = IDLE, "", "", None
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
		@during_maintenance.

		:returns: None: go on; else the answer"""
		maintenance = current_app.maintenance
		if not maintenance.writes_blocked:
			return None
		# no endpoint (a 404): no view, so the maintenance page
		view = current_app.view_functions.get(request.endpoint or "")
		if request.endpoint in ALWAYS_SERVED or getattr(view, "during_maintenance", False):
			return None
		info = maintenance.snapshot()
		message = f"NetRollout is under maintenance - {info['what']}. Try again in a few minutes."
		if request.method != "GET" or request.is_json or \
				request.headers.get("X-Requested-With") == "XMLHttpRequest" or \
				request.headers.get("X-NR-Background") == "1" or request.args.get("_bg") == "1":
			answer: ResponseReturnValue = err(message, 503, maintenance=True)
		else:
			answer = render_template("maintenance.html", info=info), 503
		response = current_app.make_response(answer)
		response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
		return response

	@app.context_processor
	def maintenance_banner() -> dict[str, Any]:
		"""The banner's details while waiting (locked: the pages aren't served)."""
		info = app.maintenance.snapshot()
		return {"maintenance": info} if info["state"] == WAITING else {}
