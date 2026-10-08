"""Building the web app at start: create_app() with every page, the secret
and encryption checks, the services (backend, orchestrator, web helpers,
shutdown, maintenance, database move), sessions in Redis, the request hooks,
the metrics, and the clean start (everyone signed out, leftover rollouts
cleared)."""
import os
import secrets
from collections.abc import Iterator, Mapping
from typing import Any

from flask import Flask, Request, Response
from flask.sessions import SessionMixin
from flask_session.redis import RedisSessionInterface
from prometheus_client.core import REGISTRY, GaugeMetricFamily
from sqlalchemy.exc import OperationalError
from werkzeug.middleware.proxy_fix import ProxyFix

from src.access.nginx import seed_hostname_from_site, sync_at_start
from src.accounts.users import SESSION_PREFIX, clear_sessions
from src.db.connections import BackendServices, REDIS_UNAVAILABLE, RedisConnection
from src.encryption import init_encryption, require_key_in_container
from src.jobs import JobStore, RolloutOrchestrator, clear_stale_jobs
from src.rollout.engine import endpoint
from src.runtime import VERSION, StartupError, in_container, source_url
from src.webapp.app import NetRolloutApp
from src.webapp.blueprints.admin_audit import bp as admin_audit_bp
from src.webapp.blueprints.admin_ldap import bp as admin_ldap_bp
from src.webapp.blueprints.admin_servers import bp as admin_servers_bp
from src.webapp.blueprints.admin_settings import (backups_bp as admin_backups_bp,
                                                  bp as admin_settings_bp)
from src.webapp.blueprints.admin_users import bp as admin_users_bp
from src.webapp.blueprints.analytics import admin_bp as admin_observability_bp, bp as analytics_bp
from src.webapp.blueprints.auth import bp as auth_bp
from src.webapp.blueprints.inventory import bp as inventory_bp
from src.webapp.blueprints.jobs import bp as jobs_bp
from src.webapp.blueprints.mappings import bp as mappings_bp, properties_bp
from src.webapp.blueprints.rollout import bp as rollout_bp
from src.webapp.blueprints.security import bp as security_bp
from src.webapp.blueprints.system import bp as system_bp
from src.webapp.db_move import DatabaseMove, register_maintenance
from src.webapp.hooks import register_extensions, register_handlers, register_auth
from src.webapp.http import WebServices
from src.webapp.lifecycle import Shutdown
from src.webapp.startup import new_instance_token


_CDN = "https://cdn.simpleicons.org"
# Simple Icons has no Arista, Aruba or Check Point logo: those come from the
# dashboard-icons collection (jsDelivr) and Wikimedia Commons. Arista's is a
# dark wordmark - operator_base.html shows it white on the dark theme.
_ICONS = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg"
ARISTA_LOGO = "https://upload.wikimedia.org/wikipedia/commons/9/99/Arista-networks-logo.svg"
VENDOR_LOGOS = {
	'cisco_ios': f'{_CDN}/cisco',
	'cisco_xe': f'{_CDN}/cisco',
	'cisco_xr': f'{_CDN}/cisco',
	'cisco_nxos': f'{_CDN}/cisco',
	'juniper_junos': f'{_CDN}/junipernetworks',
	'arista_eos': ARISTA_LOGO,
	'fortinet': f'{_CDN}/fortinet',
	'paloalto_panos': f'{_CDN}/paloaltonetworks',
	'aruba_aoscx': f'{_ICONS}/aruba.svg',
	'checkpoint_gaia': f'{_ICONS}/check-point.svg',
	'hp_procurve': f'{_CDN}/hp',
	'hp_comware': f'{_CDN}/hp',
}


class _SafeRedisSessionInterface(RedisSessionInterface):
	"""flask_session's Redis sessions, kept working when Redis isn't: a
	request then gets an empty session (signed out) instead of an error, and
	the live client is used per request (a Redis switch replaces it)."""

	def __init__(self, app: Flask, backend: BackendServices, **kwargs: Any) -> None:
		""":param kwargs: as RedisSessionInterface's (key_prefix, permanent)"""
		self._backend = backend
		super().__init__(app, client=backend.redis.client, **kwargs)

	# The live client, looked up per request: a Server Management Redis switch
	# closes the old one, and holding it logged everyone out until a restart
	@property
	def client(self) -> Any:
		return self._backend.redis.client

	@client.setter
	def client(self, _value: Any) -> None:
		pass   # set by the parent's __init__; the backend's is always used

	def open_session(self, app: Flask, request: Request) -> SessionMixin:
		""":returns: the request's session; an empty one when Redis is down"""
		try:
			return super().open_session(app, request)
		except REDIS_UNAVAILABLE:
			return self.session_class()

	def save_session(self, app: Flask, session: SessionMixin,
	                 response: Response) -> None:
		"""Store the session; skipped when Redis is down (signed out next time)."""
		try:
			super().save_session(app, session, response)
		except REDIS_UNAVAILABLE:
			pass


def resolve_secret_key(env: Mapping[str, str] | None = None) -> str:
	"""SECRET_KEY signs the session cookies: a known value would let anyone
	forge a sign-in, so there is no built-in default.

	:param env: where to read it; the environment when None
	:returns: the key - a random one per run in development
	:raises StartupError: missing in a container (the installer generates it)"""
	key = (env if env is not None else os.environ).get("SECRET_KEY", "").strip()
	if key:
		return key
	if in_container():
		raise StartupError(
			"SECRET_KEY is not set. In Docker it comes from the installation's "
			".env (the installer generates it). Restore it in .env and start "
			"again.")
	# Development: sessions are cleared at every start anyway (clear_sessions)
	print("[NetRollout] SECRET_KEY is not set — using a random key for this "
	      "run (development only).", flush=True)
	return secrets.token_hex(32)


def configure_app(app: Flask, secret_key: str) -> None:
	"""Flask's settings: the secret, the session cookie (Secure, HttpOnly,
	SameSite=Lax - the sessions live in Redis: launch_app's interface), the
	proxy headers nginx sets, and the templates' globals (vendor logos,
	version, source link, monitoring)."""
	app.config["SECRET_KEY"] = secret_key

	app.config["SESSION_COOKIE_SECURE"] = True
	app.config["SESSION_COOKIE_HTTPONLY"] = True
	app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

	# Flask's documented way to add WSGI middleware (mypy sees a method replaced)
	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
	app.jinja_env.globals['VENDOR_LOGOS'] = VENDOR_LOGOS
	app.jinja_env.filters['endpoint'] = endpoint     # {{ ip | endpoint(port) }}
	app.jinja_env.globals['NR_VERSION'] = VERSION        # the footer
	app.jinja_env.globals['NR_SOURCE_URL'] = source_url()
	# compose passes COMPOSE_PROFILES: Grafana runs (at /grafana/) with "monitoring"
	app.jinja_env.globals['NR_MONITORING'] = "monitoring" in [
		p.strip() for p in os.environ.get("NETROLLOUT_MONITORING", "").split(",")]


class RolloutSessionCollector:
	"""Prometheus' rollout gauges, read from Redis at each scrape:
	netrollout_active_jobs and netrollout_pending_jobs."""

	def __init__(self, redis_conn: RedisConnection) -> None:
		self.redis = redis_conn

	def collect(self) -> Iterator[GaugeMetricFamily]:
		""":returns: the two gauges - 0 when Redis is down"""
		try:
			active, pending = JobStore(self.redis).counts()
		except REDIS_UNAVAILABLE:
			active, pending = 0, 0

		active_metric = GaugeMetricFamily(
			"netrollout_active_jobs",
			"Jobs currently executing"
		)
		active_metric.add_metric([], active)
		yield active_metric

		pending_metric = GaugeMetricFamily(
			"netrollout_pending_jobs",
			"Jobs waiting in queue"
		)
		pending_metric.add_metric([], pending)
		yield pending_metric


def register_metrics(redis_conn: RedisConnection) -> None:
	"""The rollout gauges join Prometheus' registry (/metrics)."""
	REGISTRY.register(RolloutSessionCollector(redis_conn))


def register_server_state(app: NetRolloutApp) -> None:
	"""Every page gets the server's state, to show the stop / restart banner
	while it drains."""
	@app.context_processor
	def server_state() -> dict[str, Any]:
		if not app.orchestrator.draining:
			return {"server_draining": False}
		return {"server_draining": True,
		        "server_restarting": app.shutdown.restarting,
		        "server_running_rollouts": app.orchestrator.counts()["running"]}


def init_app_encryption(backend: BackendServices) -> None:
	"""Load the encryption key and check it against a stored credential (an
	unreachable database: not checked, said so).

	:raises EncryptionStartupError: the key is malformed, missing while
	 encrypted data exists, or doesn't match the stored data"""
	try:
		sample = backend.encrypted_sample()
		db_checked = True
	except OperationalError:
		sample, db_checked = None, False
	init_encryption(sample, db_checked=db_checked)


def launch_app() -> NetRolloutApp:
	"""Build the app with its services, in the order they depend on each
	other; the caller serves it.

	:raises StartupError: a secret missing in a container, a bad encryption
	 key - NetRollout must not start"""
	# Before touching any service: a missing secret must stop the start
	secret_key = resolve_secret_key()
	require_key_in_container()
	# the installer hands the hostname over in site.env (not .env): seed it
	# before BackendServices seeds the settings
	seed_hostname_from_site()
	backend = BackendServices()
	init_app_encryption(backend)
	# restart-only settings: what this process runs with (System Settings
	# shows "restart pending" while the saved value differs)
	started_with = backend.settings.restart_only_values()
	orchestrator = RolloutOrchestrator(backend,
	                                   started_with["orchestrator_workers"])
	web_services = WebServices(backend)
	app = NetRolloutApp(__name__, template_folder='../../templates',
	                   static_folder='../static')

	# per-run identity for the startup reverse-proxy check (startup.py)
	app.config["INSTANCE_TOKEN"] = new_instance_token()
	app.config["SETTINGS_STARTED_WITH"] = started_with
	app.backend = backend
	app.orchestrator = orchestrator
	app.shutdown = Shutdown(orchestrator)
	app.web = web_services


	configure_app(app, secret_key)
	# first of the request hooks: while a move copies the data, nothing writes
	register_maintenance(app)
	app.db_move = DatabaseMove(app)   # Server Management → Database → Move
	register_extensions(app)
	register_auth(app)
	register_metrics(app.backend.redis)
	register_server_state(app)

	register_handlers(app, backend)

	app.session_interface = _SafeRedisSessionInterface(
		app,
		app.backend,
		key_prefix=SESSION_PREFIX,
		permanent=False,
	)
	clear_sessions(app.backend.redis)
	clear_stale_jobs(app.backend.redis)
	# nginx serves the saved hostname (deploy/nginx's watcher applies it)
	sync_at_start(app.backend.settings)

	return app


def create_app() -> NetRolloutApp:
	"""The app with its services and every blueprint.

	:raises StartupError: NetRollout must not start (a missing secret, a
	 bad encryption key)"""
	net_rollout = launch_app()
	net_rollout.register_blueprint(auth_bp)
	net_rollout.register_blueprint(rollout_bp)
	net_rollout.register_blueprint(inventory_bp)
	net_rollout.register_blueprint(security_bp)
	net_rollout.register_blueprint(mappings_bp)
	net_rollout.register_blueprint(properties_bp)
	net_rollout.register_blueprint(analytics_bp)
	net_rollout.register_blueprint(admin_users_bp)
	net_rollout.register_blueprint(admin_servers_bp)
	net_rollout.register_blueprint(admin_ldap_bp)
	net_rollout.register_blueprint(admin_observability_bp)
	net_rollout.register_blueprint(admin_audit_bp)

	net_rollout.register_blueprint(jobs_bp)
	net_rollout.register_blueprint(system_bp)
	net_rollout.register_blueprint(admin_settings_bp)
	net_rollout.register_blueprint(admin_backups_bp)
	return net_rollout
