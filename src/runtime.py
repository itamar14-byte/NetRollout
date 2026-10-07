"""How and where this NetRollout process runs: its version, whether it's in
the Docker image, the folders it keeps its files in, and its small status
files (read_json / write_json).

Everything is read at call time, so a test (or the image) only has to set
the environment variables.
"""
import json
import os
import sys
from pathlib import Path
from typing import Any

FALLBACK_VERSION = "0.0.0.dev0"


def _read_version() -> str:
	"""The VERSION file at the repo root — the one source of the version: the
	image copies it next to src/, the CLI .exe bundles it (sys._MEIPASS).
	Between releases it names the next one (1.0.1.dev0)."""
	base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
	try:
		text = (base / "VERSION").read_text(encoding="utf-8").strip()
	except OSError:
		return FALLBACK_VERSION
	return text.removeprefix("v") or FALLBACK_VERSION


VERSION = _read_version()
SOURCE_REPO = "https://github.com/itamar14-byte/NetRollout"


def source_url() -> str:
	"""Where the source of *this* version is (AGPL-3.0 §13: offered to every
	network user): its release tag, or the repository for a dev build."""
	if "dev" in VERSION:
		return SOURCE_REPO
	return f"{SOURCE_REPO}/tree/v{VERSION}"


# ── Deployment ───────────────────────────────────────────────────────────────
# The Docker image sets NETROLLOUT_DEPLOYMENT=docker; anything else is a
# development run from the repo (or the CLI .exe). In a container secrets
# must be supplied (never generated or defaulted), the admin Restart exits and
# lets the restart policy bring it back, and startup doesn't probe the proxy.

DEPLOYMENT_ENV = "NETROLLOUT_DEPLOYMENT"
# Seconds a stop / Restart waits for running rollouts before cancelling them;
# compose's stop_grace_period must be longer (the cancel still has to record)
DRAIN_SECONDS_ENV = "NETROLLOUT_DRAIN_SECONDS"
DEFAULT_DRAIN_SECONDS = 600


class StartupError(Exception):
	"""Raised when the app must refuse to start (e.g. a required secret is
	missing in a container); the entry point prints it without a traceback."""
	pass


def in_container() -> bool:
	return os.environ.get(DEPLOYMENT_ENV, "").strip().lower() == "docker"


# Waitress worker threads. A live rollout log holds one for the whole rollout,
# and every Grafana request makes a quick sign-in check — with Waitress's
# default of 4, four open live logs stalled every other request. The threads
# mostly wait (Redis, Postgres), so many cost little.
THREADS_ENV = "NETROLLOUT_THREADS"
DEFAULT_THREADS = 32


def server_threads() -> int:
	try:
		return min(max(int(os.environ.get(THREADS_ENV, DEFAULT_THREADS)), 4), 256)
	except ValueError:
		return DEFAULT_THREADS


def drain_seconds() -> float:
	try:
		return max(0.0, float(os.environ.get(DRAIN_SECONDS_ENV,
		                                     DEFAULT_DRAIN_SECONDS)))
	except ValueError:
		return float(DEFAULT_DRAIN_SECONDS)


# ── Folders ──────────────────────────────────────────────────────────────────
# The base folder, in order: NETROLLOUT_HOME if set (the image: /data), the
# executable's folder when frozen (the CLI .exe: logs next to it), else the
# repo root (development).

HOME_ENV = "NETROLLOUT_HOME"
REPO_ROOT = Path(__file__).resolve().parent.parent


def home() -> Path:
	if os.environ.get(HOME_ENV):
		return Path(os.environ[HOME_ENV])
	if getattr(sys, "frozen", False):
		return Path(sys.executable).resolve().parent
	return REPO_ROOT


def logs_dir() -> Path:
	return home() / "logs"


def config_dir() -> Path:
	return home() / "config"


def certs_dir() -> Path:
	return home() / "certs"


def runtime_env() -> Path:
	"""The app-owned config file: written only by Server Management (a
	database / Redis switch) and loaded over the container environment."""
	return config_dir() / "runtime.env"


def backups_dir() -> Path:
	return home() / "backups"


def grafana_dir() -> Path:
	"""Grafana's data volume, mounted read-only into the app (the backup copies
	its database); absent in development and without monitoring."""
	return home() / "grafana"


def read_json(path: Path) -> dict[str, Any] | None:
	""":returns: the file's JSON object; None when missing, unreadable or not one"""
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None
	return data if isinstance(data, dict) else None


def write_json(path: Path, data: dict[str, Any]) -> None:
	"""Replace a status file: written aside, then renamed - a reader never
	sees half of it. The folder is made when missing."""
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_name(path.name + ".tmp")
	tmp.write_text(json.dumps(data), encoding="utf-8")
	os.replace(tmp, path)
