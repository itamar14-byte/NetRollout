import uuid

import flask_wtf.csrf as csrf_err
from flask import request, redirect, url_for, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from prometheus_flask_exporter import PrometheusMetrics
from redis.exceptions import ConnectionError as RedisConnectionError, \
	TimeoutError as RedisTimeoutError
from sqlalchemy.exc import OperationalError

from src.db.backend import BackendServices
from src.db.tables import User
from src.encryption import InvalidEncryptionKeyError
from src.webapp.utils import err

login_mng = LoginManager()
login_mng.login_view = "auth.home"
conn_limit = Limiter(get_remote_address, default_limits=[],
                     storage_uri="memory://")
csrf = CSRFProtect()


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
		fail the request cleanly instead of a bare 500."""
		if request.is_json:
			return err("Encryption key invalid", 500)
		return render_template("key_error.html", error_message=str(e)), 500
