"""What isn't a page: the health check, the instance token (the startup
proxy check) and Grafana's sign-in check for nginx. No sign-in needed; all
still answer during maintenance."""
from typing import Any

from flask import Blueprint, Response, jsonify
from flask.typing import ResponseReturnValue
from flask_login import current_user

from src.db.tables import Role
from src.runtime import VERSION
from src.webapp.app import current_app
from src.webapp.lifecycle import during_maintenance
from src.webapp.startup import GRAFANA_AUTH_PATH, HEALTH_PATH, INSTANCE_PATH

bp = Blueprint("system", __name__)


@bp.route(GRAFANA_AUTH_PATH)
@during_maintenance    # reads only; the dashboards stay
def grafana_auth() -> Response:
	"""nginx asks this before every /grafana/ request (auth_request), with the
	browser's cookies: Grafana is for signed-in admins only. nginx understands
	only 2xx / 401 / 403 here — a redirect would be a 500 — so this answers
	with a status, never a page. 204 carries the username, which nginx hands
	to Grafana (proxy auth) in a header the browser can't set."""
	# (a deactivated user isn't authenticated: Flask-Login's is_authenticated
	# is User.is_active)
	if not current_user.is_authenticated:
		return Response(status=401)       # nginx sends them to sign in
	if current_user.role != Role.ADMIN or current_user.must_change_password:
		return Response(status=403)
	return Response(status=204,
	                headers={"X-NetRollout-User": current_user.username})


@bp.route(INSTANCE_PATH)
@during_maintenance
def instance() -> Response:
	"""This process's random per-run token — lets the startup check prove the
	reverse proxy forwards to *this* instance. No login, no session write, no
	DB; the token means nothing outside this process."""
	return jsonify(instance=current_app.instance_token)


@bp.route(HEALTH_PATH)
@during_maintenance
def health() -> ResponseReturnValue:
	"""For Docker's health check, `compose up --wait`, the installer and
	`netrollout status` — callers without a browser session. Up/down per
	service, rollout counts and the version; no hostnames, no error text.
	200 when Postgres and Redis are both up, else 503."""
	services = current_app.backend.health()
	up = services["POSTGRES"] and services["REDIS"]
	body = {"status": "ok" if up else "degraded",
	        "version": VERSION,
	        "postgres": services["POSTGRES"],
	        "redis": services["REDIS"],
	        "rollouts": current_app.orchestrator.counts(),
	        "draining": current_app.orchestrator.draining,
	        # a database move: still 200 - not down (Docker, the Manager)
	        "maintenance": _maintenance()}
	response = jsonify(body)
	response.headers["Cache-Control"] = "no-store"
	return response, 200 if up else 503


def _maintenance() -> dict[str, Any] | None:
	""":returns: a database move's {state, progress}; None: none"""
	if not current_app.maintenance.active:
		return None
	info = current_app.maintenance.snapshot()
	return {"state": info["state"], "progress": info["progress"]}
