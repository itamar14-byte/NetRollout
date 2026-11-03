"""The web app's request-wide machinery: sign-in (Flask-Login), CSRF, rate
limits and Prometheus metrics; the session lifetime and the forced password
change, checked before every request, and the request's side of the
sessions (the signed-in user's row, their clocks; the store is
users.SessionStore, current_app.sessions); the pages for an unavailable
service and an encryption key that doesn't match; the Content-Security-Policy
every answer carries."""
import re
import sys
import time
import uuid
from typing import Any

import flask_wtf.csrf as csrf_err
from flask import Flask, Response, flash, g, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user, login_url, logout_user
from flask_wtf import CSRFProtect
from prometheus_flask_exporter import PrometheusMetrics
from redis.exceptions import ConnectionError as RedisConnectionError, \
	TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from src.accounts.users import (ABSOLUTE_SESSION_HOURS, LAST_ACTIVE, NO_SESSION_PATHS,
                                SIGNED_IN_AT, seconds_left)
from src.audit import AuditAction
from src.db.connections import BackendServices
from src.db.tables import User
from src.encryption import ENV_VAR, KEY_FILE, InvalidEncryptionKeyError, \
	key_source
from src.webapp.app import NetRolloutApp, current_app
from src.webapp.http import Caller, err, is_background, viewer
from src.webapp.startup import CSP_REPORT_PATH


# ── The request's session ──

def session_seconds_left(now: float | None = None) -> tuple[float, float]:
	"""(idle, absolute) seconds left for the current session.

	:param now: the time (epoch seconds); now when None"""
	return seconds_left(session, now or time.time(), current_app.sessions.idle_seconds())


def mark_sessions_unavailable() -> None:
	"""Redis couldn't be read for this request's session (the session
	interface, build.py): the request is answered with the service-unavailable
	page (register_auth) instead of running signed out."""
	g.sessions_unavailable = True


def sessions_unavailable() -> bool:
	""":returns: whether this request's session couldn't be read"""
	return bool(g.get("sessions_unavailable"))


def mark_signed_in() -> None:
	"""A sign-in just completed: both clocks start now."""
	now = time.time()
	session[SIGNED_IN_AT] = session[LAST_ACTIVE] = now


def signed_in_user(db_session: Session) -> User:
	"""The signed-in user's row in this session (current_user is a detached
	copy, without its relationships).

	:raises LookupError: the account is gone (deleted while signed in)"""
	user = db_session.get(User, current_user.id)
	if user is None:
		raise LookupError("the signed-in account no longer exists")
	return user


login_mng = LoginManager()

login_mng.login_view = "auth.home"


@login_mng.unauthorized_handler
def signed_out() -> ResponseReturnValue:
	"""A signed-out request to a page that needs a sign-in. A page's script
	(a background poll, a fetch with a JSON body or the XHR header) gets 401
	JSON: it can't show the sign-in page, and following a redirect there
	would store its own path as the page to return to after signing in.
	Anything else - a link, a reload, a form posted from a page left open,
	Active Jobs' automatic ?_bg=1 reload - gets the sign-in page with ?next=,
	as Flask-Login would."""
	if Caller.SCRIPT.wants_json() or request.headers.get("X-NR-Background") == "1":
		return err("You're signed out - sign in again", 401, redirect=url_for("auth.home"))
	flash(login_mng.login_message, login_mng.login_message_category)
	return redirect(login_url(login_mng.login_view, request.url))
conn_limit = Limiter(get_remote_address, default_limits=[],
                     storage_uri="memory://")
csrf = CSRFProtect()
# Reachable while a password change is pending (must_change_password)
PASSWORD_CHANGE_ALLOWED = {"auth.change_password", "auth.logout", "static",
                           "system.instance", "system.health", "system.csp_report",
                           "system.grafana_auth",   # answers 403 itself
                           "auth.session_state"}


def register_extensions(app: Flask) -> None:
	"""Flask-Login, the rate limiter, CSRF protection and Prometheus' /metrics."""
	PrometheusMetrics(group_by='url_rule', app=app)   # adds /metrics and its hooks
	login_mng.init_app(app)
	conn_limit.init_app(app)
	csrf.init_app(app)


# ── The Content-Security-Policy ──

# The pages' scripts are files of NetRollout's own (src/static: no inline
# script, no on...= handler, no eval - QueryBuilder's templates come compiled);
# styles may be inline (the pages' style= attributes); images also data: (the
# 2FA QR code, CSS icons); nothing from another site. The port change's probe
# (System Settings) asks this host on the new port: connect-src names it.
# frame-ancestors is nginx's X-Frame-Options SAMEORIGIN, for dev without nginx
# too. Grafana (/grafana/) isn't Flask's and doesn't get it.
CSP_HEADER = "Content-Security-Policy"
CSP_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
              "img-src 'self' data:; font-src 'self'; connect-src 'self'{trial}; "
              "object-src 'none'; base-uri 'self'; frame-ancestors 'self'; form-action 'self'; "
              "report-uri " + CSP_REPORT_PATH)
# a name or an IPv4 address (CSP has no syntax for an IPv6 one)
_CSP_HOST = re.compile(r"^[A-Za-z0-9.-]+$")


def content_security_policy(host: str) -> str:
	"""The policy for a page asked for at host.

	:param host: the request's Host (name[:port]); its name is allowed on
	 any port for connect-src - the port change's probe - when CSP can say it
	:returns: the header's value"""
	name = host.rsplit(":", 1)[0] if not host.startswith("[") else ""
	trial = f" https://{name}:*" if _CSP_HOST.match(name) else ""
	return CSP_POLICY.format(trial=trial)


def register_security_headers(app: Flask, header: str = CSP_HEADER) -> None:
	"""Every answer carries the Content-Security-Policy.

	:param header: Content-Security-Policy, or -Report-Only to only report"""
	@app.after_request
	def add_content_security_policy(response: Response) -> Response:
		response.headers.setdefault(header, content_security_policy(request.host))
		return response


def register_auth(app: NetRolloutApp) -> None:
	"""Who is signed in, and the checks before every request: the sessions'
	store answering, the session's lifetime, then a pending password change."""
	@app.before_request
	def require_sessions() -> None:
		"""Redis down: no request runs without its session - a sign-in would
		be lost, a page would think its user signed out. The service-
		unavailable page (or 503 JSON) answers instead, Redis named on it.
		Static files and the paths that use no session are still served.

		:raises RedisConnectionError: the sessions' store is unavailable"""
		if sessions_unavailable() and not request.path.startswith(NO_SESSION_PATHS):
			raise RedisConnectionError("the session store (Redis) is unavailable")

	@login_mng.user_loader
	def load_user(user_id: str) -> User | None:
		""":returns: the session's user, detached; None: unknown (signed out)"""
		backend = app.backend
		with backend.postgres.get_session() as db_session:
			try:
				user = db_session.get(User, uuid.UUID(user_id))
			except ValueError:
				return None
			if user:
				db_session.expunge(user)
			return user

	@app.before_request
	def enforce_session_lifetime() -> ResponseReturnValue | None:
		"""End a session past its idle or absolute limit (audited) - then a
		page gets the sign-in (with ?next=), a request from a page 401 JSON,
		Grafana's auth check a bare 401. A request a person made counts as
		activity; a background one doesn't.

		:returns: None: go on; else the answer"""
		if request.path.startswith(NO_SESSION_PATHS) \
				or not current_user.is_authenticated:
			return None
		now = time.time()
		session.setdefault(SIGNED_IN_AT, now)     # sessions from before this
		session.setdefault(LAST_ACTIVE, now)
		idle_left, absolute_left = session_seconds_left(now)
		if idle_left > 0 and absolute_left > 0:
			if not is_background():
				session[LAST_ACTIVE] = now
			return None
		reason = "idle" if idle_left <= 0 else "absolute"
		current_app.web.audit(AuditAction.AUTH_SESSION_EXPIRED, detail={"reason": reason})
		logout_user()
		session.clear()
		if request.endpoint == "system.grafana_auth":
			return Response(status=401)           # nginx: only a status
		if Caller.SESSION_CHECK.wants_json():
			return err("Your session has ended - sign in again", 401,
			           redirect=url_for("auth.home"))
		flash(f"You were signed out after {current_app.sessions.idle_seconds() // 60} minutes "
		      f"without activity." if reason == "idle" else
		      f"Signed out: sessions last at most {ABSOLUTE_SESSION_HOURS} "
		      f"hours. Sign in again.", "info")
		return redirect(url_for("auth.home", next=request.full_path.rstrip("?")))

	@app.context_processor
	def session_lifetime_for_pages() -> dict[str, Any]:
		"""The idle limit, for the page's own countdown and warning
		(_idle_timeout.html)."""
		if not current_user.is_authenticated:
			return {}
		try:
			return {"NR_IDLE_SECONDS": current_app.sessions.idle_seconds()}
		except SQLAlchemyError:                  # Postgres down: the error page
			return {}

	@app.before_request
	def require_password_change() -> ResponseReturnValue | None:
		"""The seeded admin, and a user after an admin reset, can do nothing
		but pick their own password (or leave).

		:returns: None: go on; else the way to the change page"""
		# the endpoint first: current_user loads the user from Postgres, and
		# while Postgres is down the error page's own /static files must load
		if request.endpoint in PASSWORD_CHANGE_ALLOWED:
			return None
		if not (current_user.is_authenticated
		        and current_user.must_change_password):
			return None
		if Caller.SCRIPT.wants_json():
			return err("Change your password first", 403,
			           redirect=url_for("auth.change_password"))
		return redirect(url_for("auth.change_password"))


def register_handlers(app: Flask, backend: BackendServices) -> None:
	"""The answers to a stale form (CSRF), a page that isn't there (404), an
	unavailable Postgres or Redis, and stored data the encryption key can't
	decrypt."""

	@app.errorhandler(csrf_err.CSRFError)
	def handle_csrf_error(_: Exception) -> ResponseReturnValue:
		if Caller.JSON_BODY.wants_json():
			return err("Session expired")
		return redirect(url_for("auth.home"))

	# A page that isn't there: a person gets 404.html (signed in or not; it
	# needs no database), a script its JSON
	@app.errorhandler(404)
	def handle_not_found(_: Exception) -> ResponseReturnValue:
		if Caller.SCRIPT.wants_json():
			return err("Not found", 404)
		return render_template("404.html", path=request.path,
		                       signed_in=current_user.is_authenticated), 404

	# Unreachable (vs refusing) Redis hosts raise TimeoutError, which is not
	# a ConnectionError subclass
	@app.errorhandler(OperationalError)
	@app.errorhandler(RedisConnectionError)
	@app.errorhandler(RedisTimeoutError)
	def handle_service_unavailable(_: Exception) -> ResponseReturnValue:
		""":returns: 503 - JSON for a request from a page, else the page that
		 says which service is down and where it was looked for"""
		if Caller.STREAM_AWARE.wants_json():
			return err("a backend service is unavailable", 503)

		# the user wasn't loaded (that failed too): the page is rendered for
		# no one in particular - else the pages' context processors would try
		# again, and the error handler's own error leaves a bare 500
		if "_login_user" not in g:              # Flask-Login's per-request user
			g._login_user = login_mng.anonymous_user()
		res = backend.health()
		return render_template("db_error.html", **backend.addresses(),
		                       postgres=res["POSTGRES"],
		                       redis=res["REDIS"]), 503

	@app.errorhandler(InvalidEncryptionKeyError)
	def handle_invalid_encryption_key(e: Exception) -> ResponseReturnValue:
		"""Stored credentials can't be decrypted with the configured key -
		fail the request cleanly instead of a bare 500. Admins get the fix;
		everyone else is told to contact one. The server log always gets it
		too: a failure at 2FA sign-in means no admin can see the page."""
		area = _decrypt_area(request.endpoint, request.blueprint)
		print(f"[NetRollout] Decryption failed ({area}) at {request.path}: the "
		      f"key from {key_source()} doesn't match the stored data.\n"
		      f"  Fix: restore the key this data was saved with ({ENV_VAR}, "
		      f"or {KEY_FILE} from the previous host or a backup), then "
		      f"restart.\n"
		      f"  If it's lost: re-enter security profile passwords and the "
		      f"LDAP bind password; for 2FA, Admin -> Users -> Reset 2FA (the "
		      f"factory admin account signs in without 2FA unless it was turned "
		      f"on for it - then run netrollout reset-admin on the server first).\n"
		      f"  Don't generate a new key.", file=sys.stderr, flush=True)
		if Caller.JSON_BODY.wants_json():
			return err("Encryption key invalid", 500)
		is_admin = current_user.is_authenticated and viewer().is_admin
		# a POST can't be retried by a link: go back to where it came from
		retry = request.path if request.method == "GET" \
			else (request.referrer or "/")
		return render_template("key_error.html", is_admin=is_admin, area=area,
		                       key_source=key_source(), env_var=ENV_VAR,
		                       key_file=str(KEY_FILE), path=request.path,
		                       retry=retry, error_message=str(e)), 500


def _decrypt_area(endpoint: str | None, blueprint: str | None) -> str:
	"""Which stored secret a failed decrypt most likely belongs to, from where
	it happened - so the page can say what to re-enter."""
	if endpoint == "auth.otp_verify":
		return "2fa"
	if blueprint in ("auth", "admin_servers", "admin_ldap"):
		return "ldap"   # LDAP sign-in / server management bind password
	if blueprint in ("security", "rollout", "inventory"):
		return "profile"
	return "credentials"
