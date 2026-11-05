"""The server's one discovery (app.discovery): starting it - refused while
another runs, while NetRollout stops or moves its database -, the current
or last one with its findings (kept KEEP_FOR seconds after it ends), the
rule for the addresses never signed in to, and the audit row when it ends.
Discover devices' routes (blueprints/discovery.py) go through it."""
from __future__ import annotations   # type hints are never evaluated

import datetime
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.audit import Actor, AuditAction, AuditTrail
from src.discovery import scan as scan_module
from src.discovery.scan import KEEP_FOR, DiscoveryScan, Login, Phase, Reason, Skip
from src.discovery.targets import Targets
from src.jobs import with_owners
from src.rollout import detect as detection
from src.rollout.host_keys import OFF, HostKeyPolicy
from src.webapp.lifecycle import Maintenance

if TYPE_CHECKING:   # annotations only
	from src.db.connections import PostgresConnection
	from src.jobs import RolloutOrchestrator

_LABEL = 255   # audit_log.object_label's length


class DiscoveryRefused(Exception):
	"""A discovery can't start now; the message says why, with its HTTP status."""

	def __init__(self, message: str, code: int) -> None:
		super().__init__(message)
		self.code = code


@dataclass(frozen=True)
class Starter:
	"""The admin who started a discovery, as the audit names them."""
	user_id: uuid.UUID
	username: str
	ip: str | None


@dataclass(frozen=True)
class Discovery:
	"""A scan and what it was started with: who, which profile, and what the
	warning they confirmed said."""
	scan: DiscoveryScan
	starter: Starter
	profile_id: uuid.UUID
	profile_label: str
	sources: tuple[str, ...]    # the server's addresses the warning named

	def summary(self) -> dict[str, Any]:
		""":returns: what every page shows of it (the page, Inventory's
		 banner, the Restart dialog), as JSON"""
		scan = self.scan
		return {"phase": str(scan.phase), "by": self.starter.username,
		        "started": scan.started.isoformat(timespec="seconds"),
		        "ended": scan.ended.isoformat(timespec="seconds") if scan.ended else None,
		        "entries": list(scan.targets.entries), "port": scan.port, "profile": self.profile_label,
		        "cancelled_by": scan.cancelled_by, "counts": scan.counts()}


def skip_rule(known: Mapping[str, str], orchestrator: RolloutOrchestrator,
              postgres: PostgresConnection) -> Callable[[str], Skip | None]:
	"""Which addresses a discovery doesn't sign in to: one Inventory has
	already (read once, when it starts), or one a rollout holds (asked when
	its turn comes).

	:param known: endpoint (ip:port, a device saved by name also by its
	 address) -> the device's label, from the devices the starter sees
	:returns: endpoint -> why not, or None"""
	def skip(target: str) -> Skip | None:
		if target in known:
			return Skip(Reason.IN_INVENTORY, known[target])
		if row := orchestrator.conflicts([target]).get(target):
			(named,) = with_owners([row], postgres)
			return Skip(Reason.IN_ROLLOUT, f"{named['user']}'s rollout")
		return None

	return skip


class Discoveries:
	"""One discovery per server at a time (app.discovery); in memory, like
	the rollouts."""

	def __init__(self, orchestrator: RolloutOrchestrator, maintenance: Maintenance,
	             audit_trail: AuditTrail,
	             now: Callable[[], datetime.datetime] = datetime.datetime.now) -> None:
		self._orchestrator = orchestrator
		self._maintenance = maintenance
		self._audit_trail = audit_trail
		self._now = now
		self._lock = threading.Lock()
		self._current: Discovery | None = None
		# the port check and the sign-in each scan uses (a test's own)
		self.knock: Callable[[str, int], str | None] = scan_module.read_banner
		self.detect: Callable[..., Any] = detection.detect

	def refusal(self) -> str | None:
		""":returns: why no discovery may start now (NetRollout stopping, a
		 database move), or None"""
		if self._orchestrator.draining:
			return "NetRollout is stopping or restarting - start the discovery once it's back."
		if self._maintenance.active or self._orchestrator.paused:
			return "NetRollout is moving to another database - start the discovery after the move."
		return None

	def start(self, targets: Targets, port: int, login: Login, *, starter: Starter,
	          profile_id: uuid.UUID, profile_label: str, sources: tuple[str, ...],
	          skip: Callable[[str], Skip | None],
	          host_keys: Callable[[str], HostKeyPolicy] = lambda target: OFF) -> Discovery:
		"""Start a discovery; the last one's findings are dropped.

		:param host_keys: endpoint -> what its host key is checked against

		:raises DiscoveryRefused: 503 NetRollout is stopping or moving its
		 database, 409 another discovery is running"""
		if reason := self.refusal():
			raise DiscoveryRefused(reason, 503)
		with self._lock:
			running = self._current
			if running and running.scan.phase not in (Phase.DONE, Phase.CANCELLED):
				raise DiscoveryRefused(
					f"{running.starter.username}'s discovery is running (started "
					f"{running.scan.started:%H:%M}) - wait for it to end, or cancel it.", 409)
			scan = DiscoveryScan(targets, port, login, skip=skip, knock=self.knock, detect=self.detect,
			                     stop_when=self._stop_when, on_end=self._ended, host_keys=host_keys)
			self._current = Discovery(scan, starter, profile_id, profile_label, sources)
			scan.start()
			return self._current

	def current(self) -> Discovery | None:
		""":returns: the running discovery, or the last one until KEEP_FOR
		 seconds after it ended; None"""
		with self._lock:
			found = self._current
			if found and found.scan.ended and \
					(self._now() - found.scan.ended).total_seconds() > KEEP_FOR:
				self._current = found = None
			return found

	def cancel(self, by: str) -> bool:
		""":returns: whether a running discovery was cancelled"""
		found = self.current()
		return bool(found and found.scan.cancel(by))

	def _stop_when(self) -> str | None:
		""":returns: who stops a running scan now: a restart, a database move"""
		if self._orchestrator.draining:
			return "restart"
		return "maintenance" if self._maintenance.active or self._orchestrator.paused else None

	def _ended(self, scan: DiscoveryScan) -> None:
		"""The audit row (inventory.discover), by the starter; printed
		instead while a database move has the database locked."""
		with self._lock:
			found = self._current
		if found is None or found.scan is not scan:
			return
		counts = scan.counts()
		detail = {"entries": list(scan.targets.entries), "addresses": scan.targets.count, "port": scan.port,
		          "profile": found.profile_label, "counts": counts,
		          "ended": "cancelled" if scan.phase == Phase.CANCELLED else "finished",
		          "cancelled_by": scan.cancelled_by, "continues": counts["continues"],
		          "warning_confirmed": {"addresses": scan.targets.count, "from": list(found.sources)}}
		starter = found.starter
		if self._maintenance.writes_blocked:
			print(f"[NetRollout] not audited during maintenance: {AuditAction.INVENTORY_DISCOVER} by "
			      f"{starter.username}", flush=True)
			return
		self._audit_trail.record(Actor(starter.user_id, starter.username, starter.ip),
		                         AuditAction.INVENTORY_DISCOVER, object_type="Discovery",
		                         object_label=", ".join(scan.targets.entries)[:_LABEL], detail=detail)
