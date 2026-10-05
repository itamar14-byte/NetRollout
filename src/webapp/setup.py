# python utilities
import os
import secrets

# services
# flask
from flask import Flask
from flask_session import Session
from flask_session.redis import RedisSessionInterface
# prometheus
from prometheus_client.core import REGISTRY, GaugeMetricFamily
# sqlalchemy
from sqlalchemy.exc import OperationalError
from werkzeug.middleware.proxy_fix import ProxyFix

# local modules
from src.webapp.startup import new_instance_token
from src.webapp.extensions import register_extensions, register_handlers, \
	register_auth
from src.webapp.utils import WebServices
from src.db.backend import BackendServices
from src.db.redis_db import REDIS_UNAVAILABLE
from src.runtime import VERSION, StartupError, in_container, source_url
from src.encryption import init_encryption, require_key_in_container
from src.job_store import JobStore
from src.orchestration import RolloutOrchestrator
from src.webapp.lifecycle import Shutdown
from src.webapp.proxy_config import seed_hostname_from_site, sync_at_start

########Constants###################################################

_CDN = "https://cdn.simpleicons.org"
VENDOR_LOGOS = {
	'cisco_ios': f'{_CDN}/cisco',
	'cisco_xe': f'{_CDN}/cisco',
	'cisco_xr': f'{_CDN}/cisco',
	'cisco_nxos': f'{_CDN}/cisco',
	'juniper_junos': f'{_CDN}/junipernetworks',
	'arista_eos': f'{_CDN}/aristanetworks',
	'fortinet': f'{_CDN}/fortinet',
	'paloalto_panos': f'{_CDN}/paloaltonetworks',
	'aruba_aoscx': f'{_CDN}/arubanetworks',
	'checkpoint_gaia': f'{_CDN}/checkpoint',
	'hp_procurve': f'{_CDN}/hp',
	'hp_comware': f'{_CDN}/hp',
}


########Class definitions###################################################

class _SafeRedisSessionInterface(RedisSessionInterface):
	def __init__(self, app, backend: BackendServices, **kwargs):
		self._backend = backend
		super().__init__(app, client=backend.redis.client, **kwargs)

	# The live client, looked up per request: a Server Management Redis switch
	# closes the old one, and holding it logged everyone out until a restart
	@property
	def client(self):
		return self._backend.redis.client

	@client.setter
	def client(self, _value):
		pass   # set by the parent's __init__; the backend's is always used

	def open_session(self, app, request):
		try:
			return super().open_session(app, request)
		except REDIS_UNAVAILABLE:
			return self.session_class()

	def save_session(self, app, session, response):
		try:
			super().save_session(app, session, response)
		except REDIS_UNAVAILABLE:
			pass


def resolve_secret_key(env=None) -> str:
	"""SECRET_KEY signs the session cookies: a known value would let anyone
	forge a sign-in, so there is no built-in default.
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


def configure_app(app, redis, secret_key: str):
	app.config["SECRET_KEY"] = secret_key

	app.config["SESSION_TYPE"] = "redis"
	app.config["SESSION_REDIS"] = redis.client
	app.config["SESSION_KEY_PREFIX"] = "redis_session:"
	app.config["SESSION_PERMANENT"] = False

	app.config["SESSION_COOKIE_SECURE"] = True
	app.config["SESSION_COOKIE_HTTPONLY"] = True
	app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

	Session(app)

	app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
	app.jinja_env.globals['VENDOR_LOGOS'] = VENDOR_LOGOS
	app.jinja_env.globals['NR_VERSION'] = VERSION        # the footer
	app.jinja_env.globals['NR_SOURCE_URL'] = source_url()
	# compose passes COMPOSE_PROFILES: Grafana runs (at /grafana/) with "monitoring"
	app.jinja_env.globals['NR_MONITORING'] = "monitoring" in [
		p.strip() for p in os.environ.get("NETROLLOUT_MONITORING", "").split(",")]


# set up prometheus scraping
class RolloutSessionCollector:
	def __init__(self, redis_conn):
		self.redis = redis_conn


	def collect(self):
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


def register_metrics(redis_conn):
	REGISTRY.register(RolloutSessionCollector(redis_conn))


def register_server_state(app):
	# Every page shows the stop / restart banner while the server drains
	@app.context_processor
	def server_state():
		if not app.orchestrator.draining:
			return {"server_draining": False}
		return {"server_draining": True,
		        "server_restarting": app.shutdown.restarting,
		        "server_running_rollouts": app.orchestrator.counts()["running"]}


def init_app_encryption(backend: BackendServices):
	# Fail fast: raises EncryptionStartupError if the key is malformed,
	# missing while encrypted data exists, or doesn't match stored data
	try:
		sample = backend.encrypted_sample()
		db_checked = True
	except OperationalError:
		sample, db_checked = None, False
	init_encryption(sample, db_checked=db_checked)


def clear_stale_jobs(redis_conn):
	"""Rollout jobs live only in the process running them: any job state in
	Redis at startup is left over from a crash and would show as a job that
	never ends (and skew the metrics). Never stops the start."""
	try:
		cleared = JobStore(redis_conn).reset_stale()
	except REDIS_UNAVAILABLE as e:
		print(f"[NetRollout] Leftover rollout state not cleared: Redis "
		      f"unavailable ({e})", flush=True)
		return
	if cleared:
		print(f"[NetRollout] Cleared {cleared} rollout(s) left over from a "
		      f"previous run that didn't stop cleanly", flush=True)


def clear_sessions(redis_conn):
	"""Every start signs everyone out — deliberately (2026-10-04): a privileged
	network-management console starts clean after a restart, update or
	reboot, like a firewall's management plane. Rollouts don't depend on
	sessions (the drain lets them finish). Within a run, sessions end after
	inactivity (session_idle_minutes) and after 12 hours (extensions.py)."""
	try:
		for redis_key in redis_conn.client.scan_iter("redis_session:*"):
			redis_conn.client.delete(redis_key)
	except REDIS_UNAVAILABLE:
		pass


###########App initialization#########################################
def launch_app():
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
	app = Flask(__name__, template_folder='../../templates',
	            static_folder='../static')

	# per-run identity for the startup reverse-proxy check (startup.py)
	app.config["INSTANCE_TOKEN"] = new_instance_token()
	app.config["SETTINGS_STARTED_WITH"] = started_with
	app.backend = backend
	app.orchestrator = orchestrator
	app.shutdown = Shutdown(orchestrator)
	app.web = web_services


	configure_app(app, app.backend.redis, secret_key)
	register_extensions(app)
	register_auth(app)
	register_metrics(app.backend.redis)
	register_server_state(app)

	register_handlers(app, backend)

	app.session_interface = _SafeRedisSessionInterface(
		app,
		app.backend,
		key_prefix="redis_session:",
		permanent=False,
	)
	clear_sessions(app.backend.redis)
	clear_stale_jobs(app.backend.redis)
	# nginx serves the saved hostname (deploy/nginx's watcher applies it)
	sync_at_start(app.backend.settings)

	return app

#########################################################################
