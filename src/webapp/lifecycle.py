"""The one way this process stops on purpose — a SIGTERM (docker stop,
netrollout stop / update) or the admin Restart: drain the orchestrator, then
exit. In a container the restart policy (unless-stopped) brings a Restart
back; in development the process relaunches itself."""
import os
import subprocess
import sys
import threading
import time

from src.runtime import in_container
from src.webapp.startup import RELAUNCH_ENV

# Lets the HTTP response to the Restart request go out before the exit
_EXIT_DELAY = 1.5


def relaunch_command() -> list[str]:
	# Under `python -m src.webapp`, sys.argv[0] is the __main__.py file path —
	# relaunching that runs it as a script, where `src` isn't importable and
	# the restarted app never comes back. orig_argv keeps the original
	# invocation, `-m src.webapp` included.
	return [sys.executable, *sys.orig_argv[1:]]


class Shutdown:
	def __init__(self, orchestrator):
		self._orchestrator = orchestrator
		self._lock = threading.Lock()
		self._restart: bool | None = None   # None: not begun

	@property
	def in_progress(self) -> bool:
		return self._restart is not None

	@property
	def restarting(self) -> bool:
		return bool(self._restart)

	def begin(self, deadline: float, restart: bool) -> bool:
		"""Drain (up to `deadline` seconds for running rollouts), then exit —
		in the background, so the app keeps serving pages (and the banner)
		meanwhile. :return: False if a stop is already in progress"""
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
