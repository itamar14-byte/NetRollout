import os
import sys

from waitress import serve

from src.db.settings import public_url
from src.encryption import EncryptionStartupError
from src.logging_utils import start_log_pruning, utf8_console
from src.webapp import create_app
from src.webapp.startup import start_announcer

utf8_console()

try:
	app = create_app()
except EncryptionStartupError as e:
	# Fail fast with a readable reason (e.g. in `docker logs`), not a traceback
	print(f"\n[NetRollout] Startup aborted — encryption key problem:\n"
	      f"  {e}\n", file=sys.stderr, flush=True)
	sys.exit(1)

start_log_pruning(lambda: app.backend.settings.get("log_retention_days"))
# Internal app port: set at install; nginx forwards to it
port = int(os.getenv("PORT", "8080"))
# Once Waitress answers: check the reverse proxy and print the address people
# should use (and open it in the browser on a normal desktop launch)
settings = app.backend.settings
start_announcer(app.config["INSTANCE_TOKEN"], port,
                public_setting=lambda: public_url(settings.get("public_hostname"),
                                                  settings.get("https_port")))
serve(app, host="0.0.0.0", port=port)
