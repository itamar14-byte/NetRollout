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

from flask import current_app, render_template, request

from src.webapp.utils import err

IDLE, WAITING, LOCKED = "idle", "waiting", "locked"
RETRY_AFTER_SECONDS = 10
# not views of ours: the files the pages need, Prometheus' scrape (Redis only)
ALWAYS_SERVED = {"static", "prometheus_metrics"}


def during_maintenance(view):
	"""Marks a view as still served while locked - it must not write to the
	database (the move's progress, health, ...)."""
	view.during_maintenance = True
	return view


class Maintenance:
	def __init__(self, orchestrator):
		self._orchestrator = orchestrator
		self._lock = threading.Lock()
		self._state = IDLE
		self._what = ""
		self._progress = ""
		self._actor_id = None

	@property
	def state(self) -> str:
		return self._state

	@property
	def writes_blocked(self) -> bool:
		return self._state == LOCKED

	def begin(self, what: str, actor_id) -> bool:
		"""Waiting: new rollouts paused. False when something is under way
		already (one move at a time)."""
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

	def snapshot(self) -> dict:
		return {"state": self._state, "what": self._what,
		        "progress": self._progress, "actor_id": self._actor_id}


def register_maintenance(app) -> None:
	"""app.maintenance, its gate (registered before every other request hook,
	so session and sign-in hooks can't write while locked) and the banner."""
	app.maintenance = Maintenance(app.orchestrator)

	@app.before_request
	def maintenance_gate():
		maintenance = current_app.maintenance
		if not maintenance.writes_blocked:
			return None
		view = current_app.view_functions.get(request.endpoint)
		if request.endpoint in ALWAYS_SERVED or getattr(view, "during_maintenance", False):
			return None
		info = maintenance.snapshot()
		message = f"NetRollout is under maintenance - {info['what']}. Try again in a few minutes."
		if request.method != "GET" or request.is_json or \
				request.headers.get("X-Requested-With") == "XMLHttpRequest" or \
				request.headers.get("X-NR-Background") == "1" or request.args.get("_bg") == "1":
			response = err(message, 503, maintenance=True)
		else:
			response = render_template("maintenance.html", info=info), 503
		response = current_app.make_response(response)
		response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
		return response

	@app.context_processor
	def maintenance_banner():
		info = app.maintenance.snapshot()
		return {"maintenance": info} if info["state"] == WAITING else {}
