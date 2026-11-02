"""What isn't a page: the health check, the instance token (the startup
proxy check), Grafana's sign-in check for nginx and the browsers' reports of
what the Content-Security-Policy blocked. No sign-in needed; all still answer
during maintenance."""
import json
from typing import Any

from flask import Blueprint, Response, jsonify, request
from flask.typing import ResponseReturnValue
from flask_login import current_user

from src.runtime import VERSION
from src.webapp.app import current_app
from src.webapp.hooks import conn_limit, csrf
from src.webapp.http import viewer
from src.webapp.lifecycle import during_maintenance
from src.webapp.startup import CSP_REPORT_PATH, GRAFANA_AUTH_PATH, HEALTH_PATH, INSTANCE_PATH

bp = Blueprint("system", __name__)


@bp.route(GRAFANA_AUTH_PATH)
@during_maintenance    # reads only; the dashboards stay
def grafana_auth() -> Response:
	"""nginx asks this before every /grafana/ request (auth_request), with the
	browser's cookies: Grafana is for signed-in admins only. nginx understands
	only 2xx / 401 / 403 here - a redirect would be a 500 - so this answers
	with a status, never a page. 204 carries the username, which nginx hands
	to Grafana (proxy auth) in a header the browser can't set."""
	# (a deactivated user isn't authenticated: Flask-Login's is_authenticated
	# is User.is_active)
	if not current_user.is_authenticated:
		return Response(status=401)       # nginx sends them to sign in
	if not viewer().is_admin or current_user.must_change_password:
		return Response(status=403)
	return Response(status=204,
	                headers={"X-NetRollout-User": current_user.username})


@bp.route(INSTANCE_PATH)
@during_maintenance
def instance() -> Response:
	"""This process's random per-run token - lets the startup check prove the
	reverse proxy forwards to *this* instance. No login, no session write, no
	DB; the token means nothing outside this process."""
	return jsonify(instance=current_app.instance_token)


@bp.route(HEALTH_PATH)
@during_maintenance
def health() -> ResponseReturnValue:
	"""For Docker's health check, `compose up --wait`, the installer and
	`netrollout status` - callers without a browser session. Up/down per
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


# a report is a few hundred bytes; anything bigger isn't read
CSP_REPORT_MAX_BYTES = 8192
CSP_REPORT_FIELDS = ("violated-directive", "blocked-uri", "document-uri", "source-file", "line-number")


@bp.route(CSP_REPORT_PATH, methods=["POST"])
@csrf.exempt                          # the browser sends it, without a token
@conn_limit.limit("60 per minute")
@during_maintenance                   # writes only a log line
def csp_report() -> Response:
	"""A browser reports what the page's Content-Security-Policy blocked
	(report-uri: application/csp-report JSON). One line in the app's log -
	something on a page that doesn't work. 413 when too big, 400 when not a
	report; nothing else is done with it."""
	if (request.content_length or 0) > CSP_REPORT_MAX_BYTES:
		return Response(status=413)
	try:
		report = json.loads(request.get_data(cache=False)[:CSP_REPORT_MAX_BYTES])["csp-report"]
		fields = [f"{k}={str(report.get(k, ''))[:200]!r}" for k in CSP_REPORT_FIELDS]
	except (ValueError, KeyError, TypeError, AttributeError):
		return Response(status=400)
	print("[NetRollout] Content-Security-Policy blocked: " + " ".join(fields), flush=True)
	return Response(status=204)


def _maintenance() -> dict[str, Any] | None:
	""":returns: a database move's {state, progress}; None: none"""
	if not current_app.maintenance.active:
		return None
	info = current_app.maintenance.snapshot()
	return {"state": info["state"], "progress": info["progress"]}
