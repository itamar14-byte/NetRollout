import uuid

import flask_wtf.csrf as csrf_err
from flask import request, redirect, url_for, render_template
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from prometheus_flask_exporter import PrometheusMetrics
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from src.db.backend import BackendServices
from src.db.tables import User
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
	redis = backend.redis
	postgres = backend.postgres

	_DB_HOST = postgres.engine.url.host
	_DB_PORT = postgres.engine.url.port

	_REDIS_HOST = redis.client.connection_pool.connection_kwargs.get("host",
	                                                                 "localhost")
	_REDIS_PORT = redis.client.connection_pool.connection_kwargs.get("port",
	                                                                 6379)

	@app.errorhandler(csrf_err.CSRFError)
	def handle_csrf_error(_):
		if request.is_json:
			return err("Session expired")
		return redirect(url_for("home"))

	@app.errorhandler(OperationalError)
	@app.errorhandler(RedisConnectionError)
	def handle_service_unavailable(_):
		if request.is_json or request.path.startswith('/rollout_stream'):
			return err("a backend service is unavailable", 503)

		res = backend.health()

		return render_template("db_error.html",
		                       db_host=_DB_HOST,
		                       db_port=_DB_PORT,
		                       redis_host=_REDIS_HOST,
		                       redis_port=_REDIS_PORT,
		                       postgres=res["postgres"],
		                       redis=res["redis"]), 503
