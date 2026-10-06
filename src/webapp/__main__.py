import os
import signal
import sys

from waitress import serve

from src.db.settings import public_url
from src.runtime import StartupError, drain_seconds, in_container, server_threads
from src.logging_utils import start_log_pruning, utf8_console
from src.webapp import create_app
from src.webapp.port_apply import serving_port
from src.webapp.proxy_config import start_certificate_upkeep
from src.webapp.backup_schedule import start_backup_schedule
from src.webapp.retention import start_retention_fallback
from src.webapp.startup import container_announcement, start_announcer

utf8_console()

try:
	app = create_app()
except StartupError as e:
	# Fail fast with a readable reason (e.g. in `docker logs`), not a traceback
	print(f"\n[NetRollout] Startup aborted:\n  {e}\n", file=sys.stderr,
	      flush=True)
	sys.exit(1)

# docker stop / netrollout stop / update: let running rollouts finish (up to
# the drain deadline, below compose's stop_grace_period), then exit
signal.signal(signal.SIGTERM, lambda signum, frame: app.shutdown.begin(
	drain_seconds(), restart=False))

start_log_pruning(lambda: app.backend.settings.get("log_retention_days"))
start_certificate_upkeep()   # previous hostnames leave the self-signed cert
# both wait while a database move runs (src/webapp/maintenance.py)
moving = lambda: app.maintenance.state != "idle"
start_backup_schedule(app.backend, hold=moving)   # System Settings → Backups
start_retention_fallback(app.backend, hold=moving)   # the nightly clean-up where pg_cron isn't
# Internal app port: set at install; nginx forwards to it
port = int(os.getenv("PORT", "8080"))
app.config["APP_PORT"] = port   # shown read-only in System Settings
settings = app.backend.settings


def configured_public_url():
	# the port served now — the setting is the one wanted, maybe not applied yet
	return public_url(settings.get("public_hostname"), serving_port())


if in_container():
	try:
		url = configured_public_url()
	except Exception:   # settings unavailable (e.g. DB down)
		url = None
	print("[NetRollout] " + container_announcement(url), flush=True)
else:
	# Once Waitress answers: check the reverse proxy and print the address
	# people should use (and open it in the browser on a normal desktop
	# launch)
	start_announcer(app.config["INSTANCE_TOKEN"], port,
	                public_setting=configured_public_url)
serve(app, host="0.0.0.0", port=port, threads=server_threads())
