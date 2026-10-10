"""How and where this NetRollout process runs: its version, whether it's in
the Docker image, the folders it keeps its files in, its small status files
(read_json / write_json, written atomically: write_atomic), and its
background loops (PeriodicTask).

Everything is read at call time, so a test (or the image) only has to set
the environment variables.
"""
import json
import os
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

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

# Each major release's name: a networking alphabet, one letter per major (1 -
# A, 2 - B, ...); minors and patches carry their major's (CONTRIBUTING:
# versioning). The next one is added when its major is planned.
CODENAMES = ("Anycast",)


def codename(version: str = VERSION) -> str | None:
	""":returns: the release name of the version's major (1.x.y -> CODENAMES[0]);
	 None when that major has none (a development 0.x, or one not named yet)"""
	try:
		major = int(version.split(".", 1)[0])
	except ValueError:
		return None
	return CODENAMES[major - 1] if 1 <= major <= len(CODENAMES) else None


CODENAME = codename()
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


def write_atomic(path: Path, data: bytes, mode: int) -> None:
	"""Replace a file: written to a temp file in the same folder (created
	owner-only), given `mode`, then renamed over it - a reader sees the old
	file or the new one, never half of one. The temp file goes on any failure.

	:param mode: the new file's permissions (e.g. 0o600 for a private key)"""
	fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
	try:
		with os.fdopen(fd, "wb") as f:
			f.write(data)
		os.chmod(tmp, mode)
		os.replace(tmp, path)
	except BaseException:
		if os.path.exists(tmp):
			os.remove(tmp)
		raise


def write_json(path: Path, data: dict[str, Any]) -> None:
	"""Replace a status file atomically (write_atomic; readable by all - the
	app and the host scripts read what the other wrote). The folder is made
	when missing."""
	path.parent.mkdir(parents=True, exist_ok=True)
	write_atomic(path, json.dumps(data).encode("utf-8"), 0o644)


# ── Background loops ─────────────────────────────────────────────────────────

class PeriodicTask(ABC):
	"""A daemon thread doing one thing every `interval` seconds, for as long
	as the process lives (the nightly clean-up, the backup schedule, the
	certificate upkeep): an optional first wait, then run_once() and a sleep,
	again and again. A failure never ends it: failed() reports it (FAILURE,
	printed) - and may arrange a retry. hold() is the caller's "not now" (a
	database move), asked by run_once() where it matters."""
	FAILURE: ClassVar[str]      # the failure's line, with {error}

	def __init__(self, name: str, interval: float, first_delay: float | None = None,
	             hold: Callable[[], bool] = lambda: False) -> None:
		""":param name: the thread's name
		:param interval: seconds slept after every turn
		:param first_delay: seconds slept before the first turn; None: none
		:param hold: True while the task must wait"""
		self.name = name
		self.interval = interval
		self.first_delay = first_delay
		self._hold = hold

	def hold(self) -> bool:
		""":returns: whether the task must wait now (the caller's hold)"""
		return self._hold()

	@abstractmethod
	def run_once(self) -> None:
		"""One turn (it raises: failed() reports it)."""

	def failed(self, error: Exception) -> None:
		"""A turn raised: printed (FAILURE), the loop goes on."""
		print(f"[NetRollout] {self.FAILURE.format(error=error)}", flush=True)

	def start(self) -> None:
		"""The loop, in a daemon thread. Never raises."""
		threading.Thread(target=self._loop, name=self.name, daemon=True).start()

	def _loop(self) -> None:
		if self.first_delay is not None:
			time.sleep(self.first_delay)
		while True:
			try:
				self.run_once()
			except Exception as e:      # noqa: BLE001 - keep the thread alive
				self.failed(e)
			time.sleep(self.interval)
