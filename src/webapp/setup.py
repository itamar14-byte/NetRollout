# python utilities
import os

# services
# flask
from flask import Flask
from flask_session import Session
from flask_session.redis import RedisSessionInterface
# prometheus
from prometheus_client.core import REGISTRY, GaugeMetricFamily
# redis
from redis.exceptions import ConnectionError as RedisConnectionError
# sqlalchemy
from werkzeug.middleware.proxy_fix import ProxyFix

# local modules
from src.webapp.extensions import register_extensions, register_handlers, \
	register_auth
from src.webapp.utils import WebServices
from src.db.backend import BackendServices
from src.orchestration import RolloutOrchestrator

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
	def open_session(self, redis_session_app, redis_session_request):
		try:
			return super().open_session(redis_session_app,
			                            redis_session_request)
		except RedisConnectionError:
			return self.session_class()

	def save_session(self, redis_session_app, redis_session, response):
		try:
			super().save_session(redis_session_app, redis_session, response)
		except RedisConnectionError:
			pass


def configure_app(app, redis):
	app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev")

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


# set up prometheus scraping
class RolloutSessionCollector:
	def __init__(self, redis_conn):
		self.redis = redis_conn


	def collect(self):
		try:
			active = int(self.redis.client.get("netrollout:active_count") or 0)
			pending = int(
				self.redis.client.get("netrollout:pending_count") or 0)
		except RedisConnectionError:
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


def clear_sessions(redis_conn):
	try:
		for redis_key in redis_conn.client.scan_iter("redis_session:*"):
			redis_conn.client.delete(redis_key)
	except RedisConnectionError:
		pass


###########App initialization#########################################
def launch_app():

	backend = BackendServices()
	orchestrator = RolloutOrchestrator(backend,
	                                   int(os.getenv("ORCHESTRATOR_WORKERS",
	                                                 "4")))
	web_services = WebServices(backend)
	app = Flask(__name__, template_folder='../templates')

	app.backend = backend
	app.orchestrator = orchestrator
	app.web = web_services


	configure_app(app, app.backend.redis)
	register_extensions(app)
	register_auth(app)
	register_metrics(app.backend.redis)

	register_handlers(app, backend)

	app.session_interface = _SafeRedisSessionInterface(
		app,
		client=app.backend.redis.client,
		key_prefix="redis_session:",
		permanent=False,
	)
	clear_sessions(app.backend.redis)

	return app

#########################################################################
