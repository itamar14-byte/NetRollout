"""The web app's request-wide machinery: sign-in (Flask-Login), CSRF, rate
limits and Prometheus metrics; the session lifetime and the forced password
change, checked before every request, and the request's side of the
sessions (the signed-in user's row, their clocks; the store is
users.SessionStore, current_app.sessions); the pages for an unavailable
service and an encryption key that doesn't match."""
import sys
import time
import uuid
from typing import Any

import flask_wtf.csrf as csrf_err
from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user, logout_user
from flask_wtf import CSRFProtect
from prometheus_flask_exporter import PrometheusMetrics
from redis.exceptions import ConnectionError as RedisConnectionError, \
	TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError

from sqlalchemy.orm import Session

from src.accounts.users import (ABSOLUTE_SESSION_HOURS, LAST_ACTIVE, NO_SESSION_PATHS,
                                SIGNED_IN_AT, seconds_left)
from src.audit import AuditAction
from src.db.connections import BackendServices
from src.db.tables import User, Role
from src.encryption import ENV_VAR, KEY_FILE, InvalidEncryptionKeyError, \
	key_source
from src.webapp.app import NetRolloutApp, current_app
from src.webapp.http import Caller, err, is_background


# ── The request's session ──

def idle_seconds() -> int:
	""":returns: the idle limit, seconds (SessionStore.idle_seconds)"""
	return current_app.sessions.idle_seconds()


def session_seconds_left(now: float | None = None) -> tuple[float, float]:
	"""(idle, absolute) seconds left for the current session.

	:param now: the time (epoch seconds); now when None"""
	return seconds_left(session, now or time.time(), idle_seconds())


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


def end_user_sessions(user_id: uuid.UUID | str, keep_sid: str | None = None) -> int:
	"""Sign a user out everywhere (SessionStore.end_for).

	:param keep_sid: the caller's own session (after a password change)
	:returns: how many sessions were ended"""
	return current_app.sessions.end_for(user_id, keep_sid)


def signed_in_users(now: float | None = None) -> dict[str, float]:
	"""Who is signed in now (SessionStore.signed_in).

	:param now: the time (epoch seconds); now when None
	:returns: user id → when the newest of their live sessions began"""
	return current_app.sessions.signed_in(now or time.time())


login_mng = LoginManager()

login_mng.login_view = "auth.home"
conn_limit = Limiter(get_remote_address, default_limits=[],
                     storage_uri="memory://")
csrf = CSRFProtect()
# Reachable while a password change is pending (must_change_password)
PASSWORD_CHANGE_ALLOWED = {"auth.change_password", "auth.logout", "static",
                           "system.instance", "system.health",
                           "system.grafana_auth",   # answers 403 itself
                           "auth.session_state"}


def register_extensions(app: Flask) -> None:
	"""Flask-Login, the rate limiter, CSRF protection and Prometheus' /metrics."""
	PrometheusMetrics(group_by='url_rule', app=app)   # adds /metrics and its hooks
	login_mng.init_app(app)
	conn_limit.init_app(app)
	csrf.init_app(app)


def register_auth(app: NetRolloutApp) -> None:
	"""Who is signed in, and the checks before every request: the session's
	lifetime, then a pending password change."""
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
			return err("Your session has ended — sign in again", 401,
			           redirect=url_for("auth.home"))
		flash(f"You were signed out after {idle_seconds() // 60} minutes "
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
		return {"NR_IDLE_SECONDS": idle_seconds()}

	@app.before_request
	def require_password_change() -> ResponseReturnValue | None:
		"""The seeded admin, and a user after an admin reset, can do nothing
		but pick their own password (or leave).

		:returns: None: go on; else the way to the change page"""
		if not (current_user.is_authenticated
		        and current_user.must_change_password):
			return None
		if request.endpoint in PASSWORD_CHANGE_ALLOWED:
			return None
		if Caller.SCRIPT.wants_json():
			return err("Change your password first", 403,
			           redirect=url_for("auth.change_password"))
		return redirect(url_for("auth.change_password"))


def register_handlers(app: Flask, backend: BackendServices) -> None:
	"""The answers to a stale form (CSRF), an unavailable Postgres or Redis,
	and stored data the encryption key can't decrypt."""

	@app.errorhandler(csrf_err.CSRFError)
	def handle_csrf_error(_: Exception) -> ResponseReturnValue:
		if Caller.JSON_BODY.wants_json():
			return err("Session expired")
		return redirect(url_for("auth.home"))

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

		res = backend.health()
		# Read addresses at error time — connections can be hot-swapped from
		# the Server Management page, so startup values may be stale
		pg_url = backend.postgres.engine.url
		redis_kwargs = backend.redis.client.connection_pool.connection_kwargs
		return render_template("db_error.html",
		                       db_host=pg_url.host,
		                       db_port=pg_url.port,
		                       redis_host=redis_kwargs.get("host", "localhost"),
		                       redis_port=redis_kwargs.get("port", 6379),
		                       postgres=res["POSTGRES"],
		                       redis=res["REDIS"]), 503

	@app.errorhandler(InvalidEncryptionKeyError)
	def handle_invalid_encryption_key(e: Exception) -> ResponseReturnValue:
		"""Stored credentials can't be decrypted with the configured key —
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
		      f"factory admin account signs in without 2FA).\n"
		      f"  Don't generate a new key.", file=sys.stderr, flush=True)
		if Caller.JSON_BODY.wants_json():
			return err("Encryption key invalid", 500)
		is_admin = (current_user.is_authenticated
		            and current_user.role == Role.ADMIN)
		# a POST can't be retried by a link: go back to where it came from
		retry = request.path if request.method == "GET" \
			else (request.referrer or "/")
		return render_template("key_error.html", is_admin=is_admin, area=area,
		                       key_source=key_source(), env_var=ENV_VAR,
		                       key_file=str(KEY_FILE), path=request.path,
		                       retry=retry, error_message=str(e)), 500


def _decrypt_area(endpoint: str | None, blueprint: str | None) -> str:
	"""Which stored secret a failed decrypt most likely belongs to, from where
	it happened — so the page can say what to re-enter."""
	if endpoint == "auth.otp_verify":
		return "2fa"
	if blueprint in ("auth", "admin_servers", "admin_ldap"):
		return "ldap"   # LDAP sign-in / server management bind password
	if blueprint in ("security", "rollout", "inventory"):
		return "profile"
	return "credentials"
