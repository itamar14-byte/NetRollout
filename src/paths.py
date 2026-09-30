"""Where NetRollout keeps its files. Every folder is resolved at call time, so
a test (or the image) only has to set NETROLLOUT_HOME.

The base folder, in order:
- NETROLLOUT_HOME, if set (the Docker image: /data)
- the folder of the executable, when frozen (the CLI .exe: logs next to it)
- the repo root (development)
"""
import os
import sys
from pathlib import Path

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
