"""How and where this NetRollout process runs: its version, whether it's in
the Docker image, and the folders it keeps its files in.

Everything is read at call time, so a test (or the image) only has to set
the environment variables.
"""
import os
import sys
from pathlib import Path

# The release build (stage 6/10) sets this from the vX.Y.Z tag; between
# releases it names the next one
VERSION = "1.0.0.dev0"


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
