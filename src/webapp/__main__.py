import os
import sys

from waitress import serve

from src.encryption import EncryptionStartupError
from src.webapp import create_app

try:
	app = create_app()
except EncryptionStartupError as e:
	# Fail fast with a readable reason (e.g. in `docker logs`), not a traceback
	print(f"\n[NetRollout] Startup aborted — encryption key problem:\n"
	      f"  {e}\n", file=sys.stderr, flush=True)
	sys.exit(1)

print("app available on 127.0.0.1:8080 or localhost:8080")
serve(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
