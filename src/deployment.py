"""How this process is deployed. The Docker image sets NETROLLOUT_DEPLOYMENT=
docker; anything else is a development run from the repo (or the CLI .exe).

Container-only behaviour keys off in_container(): secrets must be supplied
(never generated or defaulted), the admin Restart exits and lets the restart
policy bring the container back, and startup doesn't probe the reverse proxy.
"""
import os

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
