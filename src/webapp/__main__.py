import os
import signal
import sys

from waitress import serve

from src.db.settings import public_url
from src.runtime import StartupError, drain_seconds, in_container
from src.logging_utils import start_log_pruning, utf8_console
from src.webapp import create_app
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
# Internal app port: set at install; nginx forwards to it
port = int(os.getenv("PORT", "8080"))
app.config["APP_PORT"] = port   # shown read-only in System Settings
settings = app.backend.settings


def configured_public_url():
	return public_url(settings.get("public_hostname"),
	                  settings.get("https_port"))


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
serve(app, host="0.0.0.0", port=port)
