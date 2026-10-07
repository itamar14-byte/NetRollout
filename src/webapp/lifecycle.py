"""The one way this process stops on purpose — a SIGTERM (docker stop,
netrollout stop / update) or the admin Restart: drain the orchestrator, then
exit. In a container the restart policy (unless-stopped) brings a Restart
back; in development the process relaunches itself."""
import os
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

from src.runtime import in_container
from src.webapp.startup import RELAUNCH_ENV

if TYPE_CHECKING:   # annotations only: the orchestrator loads the database stack
	from src.orchestration import RolloutOrchestrator

# Lets the HTTP response to the Restart request go out before the exit
_EXIT_DELAY = 1.5


def relaunch_command() -> list[str]:
	"""The command that started this process, to start it again (development).
	Not sys.argv: under `python -m src.webapp` its [0] is the __main__.py
	path - run as a script, `src` isn't importable and the restarted app
	never comes back. orig_argv keeps `-m src.webapp`."""
	return [sys.executable, *sys.orig_argv[1:]]


class Shutdown:
	"""This process's stop or restart (app.shutdown): at most one, begun once."""

	def __init__(self, orchestrator: "RolloutOrchestrator") -> None:
		self._orchestrator = orchestrator
		self._lock = threading.Lock()
		self._restart: bool | None = None   # None: not begun

	@property
	def in_progress(self) -> bool:
		""":returns: whether a stop or restart has begun"""
		return self._restart is not None

	@property
	def restarting(self) -> bool:
		""":returns: whether what has begun is a restart"""
		return bool(self._restart)

	def begin(self, deadline: float, restart: bool) -> bool:
		"""Drain (up to `deadline` seconds for running rollouts), then exit —
		in the background, so the app keeps serving pages (and the banner)
		meanwhile.

		:param restart: start again afterwards (else stop)
		:returns: False if a stop is already in progress"""
		with self._lock:
			if self._restart is not None:
				return False
			self._restart = restart
		print(f"[NetRollout] {'Restart' if restart else 'Stop'} requested — "
		      f"new rollouts are paused", flush=True)
		threading.Thread(target=self._run, args=(deadline, restart),
		                 name="shutdown", daemon=True).start()
		return True

	def _run(self, deadline: float, restart: bool) -> None:
		"""The drain, then the exit (relaunched first in development) - the
		exit even if the drain failed."""
		try:
			self._orchestrator.drain(
				deadline, report=lambda line: print(line, flush=True))
		finally:
			if restart:   # a stop has no response waiting to go out
				time.sleep(_EXIT_DELAY)
			if restart and not in_container():
				# marker: the relaunched app doesn't open another browser tab
				subprocess.Popen(relaunch_command(),
				                 env={**os.environ, RELAUNCH_ENV: "1"})
			print("[NetRollout] Stopped", flush=True)
			os._exit(0)
