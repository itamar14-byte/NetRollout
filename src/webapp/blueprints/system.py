from flask import Blueprint, Response, current_app, jsonify
from flask_login import current_user

from src.runtime import VERSION
from src.webapp.startup import GRAFANA_AUTH_PATH, HEALTH_PATH, INSTANCE_PATH

bp = Blueprint("system", __name__)


@bp.route(GRAFANA_AUTH_PATH)
def grafana_auth():
	"""nginx asks this before every /grafana/ request (auth_request), with the
	browser's cookies: Grafana is for signed-in admins only. nginx understands
	only 2xx / 401 / 403 here — a redirect would be a 500 — so this answers
	with a status, never a page. 204 carries the username, which nginx hands
	to Grafana (proxy auth) in a header the browser can't set."""
	# (a deactivated user isn't authenticated: Flask-Login's is_authenticated
	# is User.is_active)
	if not current_user.is_authenticated:
		return Response(status=401)       # nginx sends them to sign in
	if current_user.role != "admin" or current_user.must_change_password:
		return Response(status=403)
	return Response(status=204,
	                headers={"X-NetRollout-User": current_user.username})


@bp.route(INSTANCE_PATH)
def instance():
	"""This process's random per-run token — lets the startup check prove the
	reverse proxy forwards to *this* instance. No login, no session write, no
	DB; the token means nothing outside this process."""
	return jsonify(instance=current_app.config["INSTANCE_TOKEN"])


@bp.route(HEALTH_PATH)
def health():
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
	        "draining": current_app.orchestrator.draining}
	response = jsonify(body)
	response.headers["Cache-Control"] = "no-store"
	return response, 200 if up else 503
