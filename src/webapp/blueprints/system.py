from flask import Blueprint, current_app, jsonify

from src.version import VERSION
from src.webapp.startup import HEALTH_PATH, INSTANCE_PATH

bp = Blueprint("system", __name__)


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
