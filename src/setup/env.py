"""The install's .env: what it holds (env_text: every line explained; the
generated secrets; the compose files it lists) and how it is edited after
the install - only the keys the scripts may change (SCRIPT_KEYS), the file
rewritten whole; and what an update adds when a key is missing
(UPGRADE_DEFAULTS)."""
from __future__ import annotations   # Answers / Facts: annotations only

import datetime
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet

from src import runtime
if TYPE_CHECKING:   # install imports this module
	from src.setup.install import Answers, Facts


ENV_FILE = ".env"
# The compose files of an install; port 80 → https only when it is free
COMPOSE = "compose.yaml"
COMPOSE_HTTP = "compose.http.yaml"
# A developer's stack: built from the repo, the app on the host
DEV_COMPOSE = "compose.yaml,compose.http.yaml,compose.build.yaml,compose.dev.yaml"


def compose_files(env: dict[str, str]) -> list[str]:
	""":returns: the compose files .env's COMPOSE_FILE lists (the default when
	 it has none)"""
	return [f for f in env.get("COMPOSE_FILE", COMPOSE).split(",") if f]


def env_path() -> Path:
	return runtime.home() / ENV_FILE


def generate_secrets() -> dict[str, str]:
	"""Letters and digits only: the passwords go into connection URLs."""
	def password() -> str:
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


# Every key an install's .env has, and what an update does when one is
# missing (manage.upgrade): None = it can't be made up - the data depends on
# it (a new database password or encryption key would lock the data out);
# else the value to add. A key a later version adds gets an entry here (a
# generated secret, or its default) - the tests check every key env_text
# writes is listed.
UPGRADE_DEFAULTS: dict[str, Callable[[], str] | None] = {
	"POSTGRES_PASSWORD": None,
	"NETROLLOUT_DB_PASSWORD": None,
	"GRAFANA_DB_PASSWORD": None,
	"REDIS_PASSWORD": None,
	"GRAFANA_ADMIN_PASSWORD": None,
	"SECRET_KEY": lambda: secrets.token_hex(32),   # only signs sessions
	"NETROLLOUT_ENCRYPTION_KEY": None,
	"HTTPS_PORT": lambda: "443",                  # what compose uses without it
	"TZ": lambda: "UTC",
	"NETROLLOUT_SERVER_IPS": lambda: "",
	"COMPOSE_PROFILES": lambda: "",
	"COMPOSE_PATH_SEPARATOR": lambda: ",",
	"COMPOSE_FILE": lambda: COMPOSE,
}


# The .env keys the scripts may change; never a secret
SCRIPT_KEYS = ("COMPOSE_FILE", "NETROLLOUT_SERVER_IPS", "HTTPS_PORT",
               "COMPOSE_PROFILES", "TZ")


# ── .env ──

def env_read() -> dict[str, str]:
	""":returns: .env's keys and values (comments skipped)"""
	values: dict[str, str] = {}
	for line in env_path().read_text(encoding="utf-8").splitlines():
		key, sep, value = line.partition("=")
		if sep and key and not key.startswith("#"):
			values[key.strip()] = value.strip()
	return values


def env_set(updates: dict[str, str]) -> bool:
	"""Change script-owned lines of .env in place (its comments, order and
	permissions kept — the file is rewritten, not replaced).

	:returns: whether it changed
	:raises ValueError: a key the scripts don't own (nothing written)"""
	bad = [k for k in updates if k not in SCRIPT_KEYS]
	if bad:
		raise ValueError(f"not a script-owned .env key: {', '.join(bad)}")
	return env_write(updates)


def env_write(updates: dict[str, str]) -> bool:
	"""Set the keys in .env: a line already there is changed in place, a new
	key is added at the end. Any key - env_set is the guarded way (the
	scripts' keys only); manage.restore_key writes the restored encryption
	key through this.

	:returns: whether it changed"""
	path = env_path()
	lines = path.read_text(encoding="utf-8").splitlines()
	pending, out = dict(updates), []
	for line in lines:
		key = line.partition("=")[0].strip()
		if key in pending and not line.lstrip().startswith("#"):
			out.append(f"{key}={pending.pop(key)}")
		else:
			out.append(line)
	out += [f"{k}={v}" for k, v in pending.items()]
	text = "\n".join(out) + "\n"
	if text == path.read_text(encoding="utf-8"):
		return False
	# rewritten in place, not runtime.write_atomic: a new file would lose the
	# ACLs / owner the scripts set on .env
	with open(path, "w", encoding="utf-8", newline="\n") as f:
		f.write(text)
	return True
