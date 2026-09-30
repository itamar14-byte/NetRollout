import sys
import uuid

import flask_wtf.csrf as csrf_err
from flask import request, redirect, url_for, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, current_user
from flask_wtf import CSRFProtect
from prometheus_flask_exporter import PrometheusMetrics
from redis.exceptions import ConnectionError as RedisConnectionError, \
	TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError

from src.db.backend import BackendServices
from src.db.tables import User
from src.encryption import ENV_VAR, KEY_FILE, InvalidEncryptionKeyError, \
	key_source
from src.webapp.utils import err

login_mng = LoginManager()
login_mng.login_view = "auth.home"
conn_limit = Limiter(get_remote_address, default_limits=[],
                     storage_uri="memory://")
csrf = CSRFProtect()
# Reachable while a password change is pending (must_change_password)
PASSWORD_CHANGE_ALLOWED = {"auth.change_password", "auth.logout", "static",
                           "system.instance", "system.health"}


def register_extensions(app):
	app.metrics = PrometheusMetrics(group_by='url_rule', app=app)
	login_mng.init_app(app)
	conn_limit.init_app(app)
	csrf.init_app(app)


def register_auth(app):
	@login_mng.user_loader
	def load_user(user_id):
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
	def require_password_change():
		# The seeded admin, and a user after an admin reset, can do nothing
		# but pick their own password (or leave)
		if not (current_user.is_authenticated
		        and current_user.must_change_password):
			return None
		if request.endpoint in PASSWORD_CHANGE_ALLOWED:
			return None
		if request.is_json or request.headers.get("X-Requested-With") == \
				"XMLHttpRequest":
			return err("Change your password first", 403,
			           redirect=url_for("auth.change_password"))
		return redirect(url_for("auth.change_password"))


def register_handlers(app, backend: BackendServices):

	@app.errorhandler(csrf_err.CSRFError)
	def handle_csrf_error(_):
		if request.is_json:
			return err("Session expired")
		return redirect(url_for("auth.home"))

	# Unreachable (vs refusing) Redis hosts raise TimeoutError, which is not
	# a ConnectionError subclass
	@app.errorhandler(OperationalError)
	@app.errorhandler(RedisConnectionError)
	@app.errorhandler(RedisTimeoutError)
	def handle_service_unavailable(_):
		if request.is_json or request.path.startswith('/rollout/stream'):
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
	def handle_invalid_encryption_key(e):
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
		if request.is_json:
			return err("Encryption key invalid", 500)
		is_admin = (current_user.is_authenticated
		            and current_user.role == "admin")
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
	if blueprint in ("auth", "admin_servers"):
		return "ldap"   # LDAP sign-in / server management bind password
	if blueprint in ("security", "rollout", "inventory"):
		return "profile"
	return "credentials"
