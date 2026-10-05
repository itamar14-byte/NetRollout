"""What installing writes into the install folder: the folders, the
certificate, site.env (the hostname for nginx and the first start), and
.env - last, because an existing .env means "installed": nothing is ever
overwritten."""
import datetime
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from src import certs, runtime, site_env
from src.setup.answers import Answers, Facts

ENV_FILE = ".env"
FOLDERS = ("logs", "config", "certs", "backups")
# The compose files of an install; port 80 → https only when it is free
COMPOSE = "compose.yaml"
COMPOSE_HTTP = "compose.http.yaml"
# A developer's stack: built from the repo, the app on the host
DEV_COMPOSE = "compose.yaml,compose.http.yaml,compose.build.yaml,compose.dev.yaml"


class Refused(Exception):
	"""Already installed (or set up): nothing was written."""


def env_path() -> Path:
	return runtime.home() / ENV_FILE


def generate_secrets() -> dict[str, str]:
	"""Letters and digits only: the passwords go into connection URLs."""
	def password():
		return secrets.token_hex(24)
	return {
		"POSTGRES_PASSWORD": password(),
		"NETROLLOUT_DB_PASSWORD": password(),
		"GRAFANA_DB_PASSWORD": password(),
		"REDIS_PASSWORD": password(),
		"GRAFANA_ADMIN_PASSWORD": password(),
		"SECRET_KEY": secrets.token_hex(32),
		"NETROLLOUT_ENCRYPTION_KEY": Fernet.generate_key().decode(),
	}


def env_text(answers: Answers, facts: Facts, keys: dict[str, str],
             now: datetime.datetime, version: str, port80_free: bool) -> str:
	"""The .env of an install - every line explained, for whoever opens it."""
	by = f" by {facts.account}" if facts.account else ""
	files = COMPOSE + (f",{COMPOSE_HTTP}" if port80_free else "")
	return f"""\
# NetRollout {version} — written by the installer on {now:%Y-%m-%d %H:%M}{by}.
# Licence terms accepted: NetRollout (AGPL-3.0){", Docker Desktop (Docker's terms), Windows licensing" if facts.os == "windows" else ", Docker Engine (Apache-2.0)"}.
#
# Don't edit this file: NetRollout's settings are in System Settings (web UI),
# and the scripts keep the rest up to date. It holds every secret of this
# installation — keep it private (backups include it).
# Advanced (then `netrollout start`): COMPOSE_PROFILES= (empty) turns
# monitoring off; TZ changes the containers' timezone.

# ── Secrets (generated; never reuse them elsewhere) ──────────────────────────
# Postgres superuser: first-start setup and backups only, never the app
POSTGRES_PASSWORD={keys["POSTGRES_PASSWORD"]}
# The app's database role, and Grafana's read-only one
NETROLLOUT_DB_PASSWORD={keys["NETROLLOUT_DB_PASSWORD"]}
GRAFANA_DB_PASSWORD={keys["GRAFANA_DB_PASSWORD"]}
REDIS_PASSWORD={keys["REDIS_PASSWORD"]}
# Grafana's own admin (maintenance; people sign in through NetRollout)
GRAFANA_ADMIN_PASSWORD={keys["GRAFANA_ADMIN_PASSWORD"]}
# Signs the browser sessions
SECRET_KEY={keys["SECRET_KEY"]}
# Encrypts the stored device credentials. Losing it makes them unreadable;
# NetRollout refuses to start with a different key.
NETROLLOUT_ENCRYPTION_KEY={keys["NETROLLOUT_ENCRYPTION_KEY"]}

# ── This installation ────────────────────────────────────────────────────────
# The HTTPS port people use (change it in System Settings → Access)
HTTPS_PORT={answers.https_port}
# The containers' timezone: log times, the nightly clean-up at 03:00
TZ={answers.timezone}
# This server's addresses (kept up to date by `netrollout status`)
NETROLLOUT_SERVER_IPS={",".join(facts.server_ips)}

# ── What runs ────────────────────────────────────────────────────────────────
# Monitoring: "monitoring" on, empty off
COMPOSE_PROFILES={"monitoring" if answers.monitoring else ""}
# compose.http.yaml (port 80 → https) only while port 80 is free on this
# computer — `netrollout start` turns it off and on
COMPOSE_PATH_SEPARATOR=,
COMPOSE_FILE={files}
"""


def install(answers: Answers, facts: Facts, now: datetime.datetime | None = None,
            version: str | None = None) -> list[str]:
	"""Write the install; returns what was done (one line each). Raises
	Refused when .env exists, OSError when a file can't be written (.env
	isn't written then, so the install can be run again)."""
	if env_path().exists():
		raise Refused(f"NetRollout is already installed here ({env_path()}).")
	done = []
	for name in FOLDERS:
		(runtime.home() / name).mkdir(parents=True, exist_ok=True)
	done.append("folders: " + ", ".join(FOLDERS))
	if answers.org_certificate:
		done.append("certificate: yours (checked)")
	else:
		certs.selfsigned(answers.hostname, facts.server_ips, runtime.certs_dir())
		done.append(f"certificate: self-signed for {answers.hostname}"
		            + (f" and {', '.join(facts.server_ips)}" if facts.server_ips else ""))
	site_env.update({site_env.HOSTNAME: answers.hostname,
	                 site_env.HTTPS_PORT: str(answers.https_port)})
	done.append(f"hostname: {answers.hostname}")
	text = env_text(answers, facts, generate_secrets(),
	                now or datetime.datetime.now(), version or runtime.VERSION,
	                port80_free=80 not in facts.busy_ports)
	_write_new(env_path(), text)
	done.append(f"settings: {env_path()}")
	return done


def install_dev(now: datetime.datetime | None = None) -> list[str]:
	"""A developer's setup in the repo: .env for the dev stack (built from the
	repo; the app runs on the host) and config/runtime.env pointing the host
	app at it, plus a self-signed certificate for localhost if there's none.
	Refuses when either file exists."""
	runtime_env = runtime.runtime_env()
	for path in (env_path(), runtime_env):
		if path.exists():
			raise Refused(f"{path} already exists - remove it to set up again.")
	keys = generate_secrets()
	done = []
	cert_dir = runtime.certs_dir()
	if not (cert_dir / certs.CERT_FILE).exists():
		certs.selfsigned("localhost", ["127.0.0.1"], cert_dir)
		done.append("certificate: self-signed for localhost and 127.0.0.1")
	now = now or datetime.datetime.now()
	lines = [f"# NetRollout development stack - written by `python -m src.setup "
	         f"init --dev` on {now:%Y-%m-%d %H:%M}.",
	         "# The dev stack's services; the app runs from the repo (config/runtime.env).",
	         "NETROLLOUT_VERSION=dev",
	         *(f"{k}={v}" for k, v in keys.items()),
	         "HTTPS_PORT=443", "TZ=UTC",
	         "COMPOSE_PROFILES=monitoring", "COMPOSE_PATH_SEPARATOR=,",
	         f"COMPOSE_FILE={DEV_COMPOSE}", ""]
	_write_new(env_path(), "\n".join(lines))
	done.append(f"stack settings: {env_path()}")
	# 127.0.0.1, not localhost: the stack publishes IPv4 only, and Windows
	# waits ~2 s on every refused ::1 attempt
	app = ["# The app on this computer -> the dev stack (127.0.0.1: IPv4 only).",
	       "PG_HOST=127.0.0.1", "PG_PORT=5432", "PG_NAME=netrollout",
	       "PG_USER=netrollout", f"PG_PASSWORD={keys['NETROLLOUT_DB_PASSWORD']}",
	       "DATABASE_URL=",
	       "REDIS_HOST=127.0.0.1", "REDIS_PORT=6379",
	       f"REDIS_PASSWORD={keys['REDIS_PASSWORD']}", "REDIS_URL=",
	       "# the app runs on the host, so compose can't pass this (the Grafana link)",
	       "NETROLLOUT_MONITORING=monitoring", ""]
	runtime_env.parent.mkdir(parents=True, exist_ok=True)
	_write_new(runtime_env, "\n".join(app))
	done.append(f"app settings: {runtime_env}")
	return done


def _write_new(path: Path, text: str) -> None:
	"""Create `path` (never replace one), readable by its owner only (on
	Windows the script restricts it)."""
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "x", encoding="utf-8", newline="\n") as f:
		f.write(text)
	try:
		os.chmod(path, 0o600)
	except OSError:
		pass
