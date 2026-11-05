"""One discovery: check every address's SSH port, sign in to each one that
answers and detect its platform (src/rollout/detect.py), turning each into
a finding (a card the admin creates a device from) or a line saying why
not. Its own threads; in memory, in this process, like the rollouts (a
restart ends it).

The pacing protects the network and the account: at most SIGN_INS sign-ins
at once, a start at most every SIGN_IN_GAP seconds, and a pause after
FAILURES_BEFORE_PAUSE failed sign-ins (wrong credentials) since the start
or the last Continue - one started before a Continue doesn't count after
it, so Continue never pauses at once on the sign-ins still under way."""
from __future__ import annotations   # type hints are never evaluated

import datetime
import queue
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

import netmiko

from src.discovery.targets import Targets
from src.rollout import detect as detection
from src.rollout.detect import NEIGHBOUR_NAMES, Detection
from src.rollout.engine import Device, endpoint

MAX_ADDRESSES = 4096        # one discovery's addresses, at most
MAP_LIMIT = 1024            # the page's address map, up to this many (port events)
PORT_CHECKS = 64            # port checks at once
PORT_TIMEOUT = 3.0          # seconds a port check waits to connect
BANNER_TIMEOUT = 1.0        # seconds it waits for the server's first line
SIGN_INS = 4                # sign-ins at once
SIGN_IN_GAP = 0.5           # seconds between two sign-ins' starts, at least
FAILURES_BEFORE_PAUSE = 3   # failed sign-ins since the start / the last Continue
KEEP_FOR = 3600             # seconds the findings are kept after the end (Discoveries)
_DETAIL = 200               # an error's text in a line, at most


class Phase(StrEnum):
	"""Where a scan is (the page's JSON)."""
	RUNNING = "running"
	PAUSED = "paused"           # too many failed sign-ins: waiting for Continue / Cancel
	CANCELLING = "cancelling"   # cancelled: the sign-ins under way finish
	DONE = "done"
	CANCELLED = "cancelled"


ENDED = (Phase.DONE, Phase.CANCELLED)


class Reason(StrEnum):
	"""Why an address with SSH open gave no card (the page's JSON, the audit)."""
	WRONG_CREDENTIALS = "wrong-credentials"
	TIMED_OUT = "timed-out"
	FAILED = "failed"               # another error: its text in the detail
	NOT_SUPPORTED = "not-supported"  # detected, not a platform NetRollout has: its name
	IN_INVENTORY = "in-inventory"
	IN_ROLLOUT = "in-rollout"       # its owner in the detail
	NOT_ASKED = "not-asked"         # cancelled before its turn


@dataclass(frozen=True)
class Login:
	"""The profile's credentials, decrypted; dropped when the scan ends."""
	username: str
	password: str = field(repr=False)
	secret: str = field(repr=False)


@dataclass(frozen=True)
class Skip:
	"""An address not signed in to, and why (the skip rule's answer)."""
	reason: Reason
	detail: str = ""


@dataclass
class Finding:
	"""A device found: what the page's card starts from."""
	id: int
	address: str
	port: int
	banner: str
	hostname: str | None
	platform: str | None     # one of PLATFORMS; None: the admin picks
	detected: str | None
	evidence: str | None
	problem: str | None
	created: bool = False    # a device was created from it (claim())


@dataclass(frozen=True)
class Line:
	"""An address with SSH open that gave no card."""
	address: str
	reason: Reason
	detail: str = ""


def read_banner(address: str, port: int) -> str | None:
	"""The port check: connect once (PORT_TIMEOUT) and read the server's
	first line (BANNER_TIMEOUT; an SSH server sends its version at once),
	no login.

	:returns: the first line ("" when nothing came); None: not open"""
	try:
		with socket.create_connection((address, port), timeout=PORT_TIMEOUT) as conn:
			conn.settimeout(BANNER_TIMEOUT)
			try:
				first = conn.recv(256)
			except OSError:
				return ""
	except (OSError, ValueError):
		return None
	return first.decode("utf-8", "replace").splitlines()[0].strip() if first.strip() else ""


class DiscoveryScan:
	"""One scan: its targets, its threads, its state. Everything the page
	reads goes through one lock; the events are numbered, so the page asks
	only for those after the last it has (status(since))."""

	def __init__(self, targets: Targets, port: int, login: Login, *,
	             skip: Callable[[str], Skip | None],
	             knock: Callable[[str, int], str | None] = read_banner,
	             detect: Callable[[Device], Detection] = detection.detect,
	             clock: Callable[[], float] = time.monotonic,
	             sleep: Callable[[float], None] = time.sleep,
	             on_end: Callable[[DiscoveryScan], None] | None = None) -> None:
		"""
		:param skip: endpoint (ip:port) -> why it isn't signed in to, or
		 None; asked when its sign-in's turn comes
		:param knock: the port check (read_banner; a test's own)
		:param detect: the sign-in and detection (a test's own)
		:param clock: sleep: the pacing's (a test's own)
		:param on_end: called once when the scan has ended, however"""
		self.targets = targets
		self.port = port
		self._login: Login | None = login
		self._skip, self._knock, self._detect = skip, knock, detect
		self._clock, self._sleep, self._on_end = clock, sleep, on_end
		self._lock = threading.Lock()
		self._changed = threading.Condition(self._lock)
		self._turn = threading.Lock()          # one sign-in waits for its start at a time
		self._queue: queue.PriorityQueue[tuple[int, str, str]] = queue.PriorityQueue()
		self._thread = threading.Thread(target=self._run, name="discovery", daemon=True)
		self._events: list[dict[str, Any]] = []
		self._findings: dict[int, Finding] = {}
		self._lines: list[Line] = []
		self._phase = Phase.RUNNING
		self._next_start = 0.0
		self._continues = 0          # Continue presses
		self._failures = 0           # failed sign-ins since the start / the last Continue
		self._checked = self._open = self._signing_in = self._signed_in = 0
		self.cancelled_by: str | None = None
		self.started = datetime.datetime.now()
		self.ended: datetime.datetime | None = None

	# --- what the page and the routes ask --------------------------------

	def start(self) -> None:
		self._thread.start()

	def wait(self, timeout: float | None = None) -> bool:
		""":returns: whether the scan has ended within `timeout`"""
		self._thread.join(timeout)
		return not self._thread.is_alive()

	@property
	def phase(self) -> Phase:
		with self._lock:
			return self._phase

	def proceed(self) -> bool:
		"""Continue after a pause: the failures count from here.

		:returns: whether it was paused"""
		with self._lock:
			if self._phase != Phase.PAUSED:
				return False
			self._continues += 1
			self._failures = 0
			self._set_phase(Phase.RUNNING)
			return True

	def cancel(self, by: str) -> bool:
		"""No new sign-in starts; those under way finish; the rest are "not
		asked".

		:param by: who cancelled (a username; "restart", "maintenance")
		:returns: whether it was running or paused"""
		with self._lock:
			if self._phase not in (Phase.RUNNING, Phase.PAUSED):
				return False
			self.cancelled_by = by
			self._set_phase(Phase.CANCELLING)
			return True

	def status(self, since: int = 0) -> dict[str, Any]:
		""":returns: the phase, the counts and the events after `since`
		 (their sequence numbers start at 1), as the page's JSON"""
		with self._lock:
			return {"phase": str(self._phase), "counts": self._counts(), "seq": len(self._events),
			        "events": self._events[max(since, 0):]}

	def counts(self) -> dict[str, Any]:
		with self._lock:
			return self._counts()

	def findings(self) -> list[Finding]:
		with self._lock:
			return list(self._findings.values())

	def claim(self, finding_id: int) -> Finding | None:
		"""Mark a finding as being created, once.

		:returns: it; None: no such finding, or already claimed"""
		with self._lock:
			finding = self._findings.get(finding_id)
			if finding is None or finding.created:
				return None
			finding.created = True
			return finding

	def release(self, finding_id: int) -> None:
		"""Its device wasn't created after all: it may be tried again."""
		with self._lock:
			if finding := self._findings.get(finding_id):
				finding.created = False

	@property
	def continues(self) -> int:
		with self._lock:
			return self._continues

	# --- the run ---------------------------------------------------------

	def _run(self) -> None:
		"""The port checks, the sign-ins fed as ports answer; always ends
		in DONE or CANCELLED, the login dropped, on_end called."""
		signers = [threading.Thread(target=self._sign_ins, name=f"discovery-sign-in-{n}", daemon=True)
		           for n in range(SIGN_INS)]
		pool = ThreadPoolExecutor(PORT_CHECKS, thread_name_prefix="discovery-port")
		try:
			for signer in signers:
				signer.start()
			wait([pool.submit(self._check, index, address)
			      for index, address in enumerate(self.targets.addresses())])
		finally:
			pool.shutdown(cancel_futures=True)
			for _ in signers:
				self._queue.put((self.targets.count, "", ""))    # after every address: the end
			for signer in signers:
				signer.join()
			with self._lock:
				self._login = None
				self.ended = datetime.datetime.now()
				self._set_phase(Phase.CANCELLED if self._phase == Phase.CANCELLING else Phase.DONE)
			if self._on_end:
				try:
					self._on_end(self)
				except Exception as e:
					print(f"[NetRollout] discovery: its end wasn't recorded ({e})", flush=True)

	def _check(self, index: int, address: str) -> None:
		"""One address's port; an open one joins the sign-ins in address order."""
		if self.phase == Phase.CANCELLING:
			return
		try:
			banner = self._knock(address, self.port)
		except Exception:
			banner = None
		with self._lock:
			self._checked += 1
			if banner is not None:
				self._open += 1
			if self.targets.count <= MAP_LIMIT:
				self._event("port", address=address, open=banner is not None)
		if banner is not None:
			self._queue.put((index, address, banner))

	def _sign_ins(self) -> None:
		"""A sign-in worker: the next open address in order, until the end."""
		while True:
			index, address, banner = self._queue.get()
			if index >= self.targets.count:
				return
			self._sign_in(address, banner)

	def _sign_in(self, address: str, banner: str) -> None:
		"""One address: skipped, not asked, or signed in to and detected -
		never raises (a worker never dies on one address)."""
		try:
			skip = self._skip(endpoint(address, self.port))
		except Exception as e:
			skip = Skip(Reason.FAILED, _detail(e))
		if skip:
			self._add_line(Line(address, skip.reason, skip.detail))
			return
		started = self._wait_turn()
		if started is None:
			self._add_line(Line(address, Reason.NOT_ASKED))
			return
		generation, login = started
		try:
			found = self._detect(Device(ip=address, label=address, username=login.username,
			                            password=login.password, device_type="", secret=login.secret,
			                            port=self.port))
		except netmiko.exceptions.NetmikoAuthenticationException:
			self._failed(address, generation)
		except netmiko.exceptions.NetmikoTimeoutException:
			self._add_line(Line(address, Reason.TIMED_OUT), ended=True)
		except Exception as e:
			self._add_line(Line(address, Reason.FAILED, _detail(e)), ended=True)
		else:
			self._detected(address, banner, found)

	def _wait_turn(self) -> tuple[int, Login] | None:
		"""Wait for this sign-in's start: not while paused, at most SIGN_INS
		at once (the workers), SIGN_IN_GAP after the last start.

		:returns: the Continue count it starts under and the login; None:
		 cancelled first"""
		with self._turn:
			while True:
				with self._lock:
					while self._phase == Phase.PAUSED:
						self._changed.wait()
					if self._phase != Phase.RUNNING or self._login is None:
						return None
					now = self._clock()
					if now >= self._next_start:
						self._next_start = now + SIGN_IN_GAP
						self._signing_in += 1
						return self._continues, self._login
					delay = self._next_start - now
				self._sleep(delay)

	def _failed(self, address: str, generation: int) -> None:
		"""Wrong credentials: a line, and the pause once there are enough
		since the last Continue (one started before it doesn't count)."""
		with self._lock:
			self._signing_in -= 1
			self._lines.append(line := Line(address, Reason.WRONG_CREDENTIALS))
			self._event("line", **asdict(line))
			if generation == self._continues:
				self._failures += 1
				if self._failures >= FAILURES_BEFORE_PAUSE and self._phase == Phase.RUNNING:
					self._set_phase(Phase.PAUSED)

	def _detected(self, address: str, banner: str, found: Detection) -> None:
		"""Signed in: a card, or a line when it's a neighbour NetRollout
		doesn't support."""
		if found.platform is None and found.detected is not None:
			name = NEIGHBOUR_NAMES.get(found.detected, found.detected)
			self._add_line(Line(address, Reason.NOT_SUPPORTED, name), ended=True, signed_in=True)
			return
		with self._lock:
			self._signing_in -= 1
			self._signed_in += 1
			finding = Finding(len(self._findings) + 1, address, self.port, banner, found.hostname,
			                  found.platform, found.detected, found.evidence, found.problem)
			self._findings[finding.id] = finding
			self._event("finding", **asdict(finding))

	def _add_line(self, line: Line, *, ended: bool = False, signed_in: bool = False) -> None:
		""":param ended: a sign-in that had started ended with it
		:param signed_in: and it had signed in"""
		with self._lock:
			if ended:
				self._signing_in -= 1
			if signed_in:
				self._signed_in += 1
			self._lines.append(line)
			self._event("line", **asdict(line))

	# --- under the lock --------------------------------------------------

	def _set_phase(self, phase: Phase) -> None:
		self._phase = phase
		self._event("phase", phase=str(phase))
		self._changed.notify_all()

	def _event(self, kind: str, **data: Any) -> None:
		"""One event; its values JSON's own (a Reason is a str)."""
		self._events.append({"seq": len(self._events) + 1, "kind": kind, **data})

	def _counts(self) -> dict[str, Any]:
		reasons = {str(reason): 0 for reason in Reason}
		for line in self._lines:
			reasons[str(line.reason)] += 1
		return {"addresses": self.targets.count, "checked": self._checked, "open": self._open,
		        "signing_in": self._signing_in, "signed_in": self._signed_in,
		        "found": len(self._findings), "reasons": reasons, "continues": self._continues}


def _detail(error: Exception) -> str:
	""":returns: an error's first line, short enough for a line"""
	text = str(error).strip().splitlines()
	return (text[0] if text else type(error).__name__)[:_DETAIL]
