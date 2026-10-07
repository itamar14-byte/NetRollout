"""The device inventory's rules (web app): which devices a user sees and may
edit (their own and the global ones), devices sharing an endpoint, and
whether a device is reachable from the NetRollout server.

Reachability: one fast TCP connect to ip:port - the management endpoint a
push would use, from the server's point of view (the one that matters).
Results are cached briefly in Redis, so page loads stay cheap and "recent"
means at most CACHE_TTL seconds old."""
import json
import socket
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Iterable, TypedDict, cast

import redis
from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import Session

from src.db.connections import REDIS_UNAVAILABLE
from src.db.tables import Inventory, User
from src.rollout.engine import endpoint


PROBE_TIMEOUT = 2.0   # seconds; single attempt — this is a status hint
CACHE_TTL = 60        # seconds
MAX_PARALLEL = 32

Target = tuple[str, int]      # (ip, port)


class Reach(TypedDict):
	"""One target's state, as cached and as the pages get it."""
	reachable: bool
	checked_at: str               # ISO 8601, UTC


def probe(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
	"""One TCP connect, no retry.

	:param timeout: seconds before an unanswered connect counts as down
	:returns: whether ip:port accepted the connection (closed at once)"""
	try:
		with socket.create_connection((ip, int(port)), timeout=timeout):
			return True
	except (OSError, ValueError):
		return False


def _cache_key(target: Target) -> str:
	""":returns: the target's Redis key"""
	return f"reach:{target[0]}:{target[1]}"


class ReachabilityChecker:
	"""Probes targets in parallel, caching the answers in Redis."""

	def __init__(self, redis_client: Callable[[], redis.Redis],
	             ttl: int | Callable[[], int] = CACHE_TTL,
	             prober: Callable[[str, int], bool] = probe):
		""":param redis_client: returns the client in use now - a Redis switch
		 replaces it, so it's resolved on every use
		:param ttl: seconds a result is reused - or a callable (the System
		 Setting), read each time results are cached
		:param prober: probes one target (a fake in tests)"""
		self._redis = redis_client
		self._ttl = ttl
		self._probe = prober

	def check(self, targets: Iterable[tuple[str, int | str]], refresh: bool = False,
	          recheck_unreachable: bool = False) -> dict[Target, Reach]:
		"""Whether each target is reachable, from the cache where it's fresh.

		:param targets: (ip, port) pairs; the port may come as text
		:param refresh: ignore the cache and probe everything (Recheck)
		:param recheck_unreachable: trust cached "reachable" results but
		 re-probe cached failures — used before blocking a rollout, so a
		 device that just came back isn't blocked by a stale result
		:returns: every target's state, by (ip, port)"""
		wanted = {(ip, int(port)) for ip, port in targets}
		results = {} if refresh else self._cached(wanted)
		if recheck_unreachable:
			results = {t: r for t, r in results.items() if r["reachable"]}
		todo = sorted(t for t in wanted if t not in results)
		if todo:
			now = datetime.now(timezone.utc).isoformat()
			with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(todo))) as ex:
				outcomes = ex.map(lambda t: self._probe(*t), todo)
				fresh: dict[Target, Reach] = {t: {"reachable": ok, "checked_at": now}
				                              for t, ok in zip(todo, outcomes)}
			self._store(fresh)
			results.update(fresh)
		return results

	def _cached(self, targets: set[Target]) -> dict[Target, Reach]:
		""":returns: the cached states of these targets ({} when Redis is
		 down: everything is probed then)"""
		if not targets:
			return {}
		ordered = sorted(targets)
		try:
			# redis-py types a reply as maybe-awaitable; this client is sync
			raw = cast(list[bytes | None], self._redis().mget([_cache_key(t) for t in ordered]))
		except REDIS_UNAVAILABLE:
			return {}
		return {t: json.loads(v) for t, v in zip(ordered, raw) if v}

	def _current_ttl(self) -> int:
		""":returns: seconds a result is reused now (the default when the
		 setting can't be read)"""
		if not callable(self._ttl):
			return self._ttl
		try:
			return int(self._ttl())
		except Exception:   # settings unreachable: the default, not an error
			return CACHE_TTL

	def _store(self, results: dict[Target, Reach]) -> None:
		"""Cache fresh results (skipped when Redis is down)."""
		ttl = self._current_ttl()
		try:
			pipe = self._redis().pipeline()
			for t, r in results.items():
				pipe.setex(_cache_key(t), ttl, json.dumps(r))
			pipe.execute()
		except REDIS_UNAVAILABLE:
			pass


#######################Device visibility###############################
def visible_devices_clause(user_id: uuid.UUID) -> ColumnElement[bool]:
	"""Devices a user may see and roll out to: their own plus all global ones."""
	return or_(Inventory.user_id == user_id, Inventory.is_global.is_(True))


def query_visible_devices(db_session: Session, user_id: uuid.UUID) -> list[Inventory]:
	"""Visible devices with the relationships templates and rollout need,
	preloaded so rows survive expunge."""
	devices = (db_session.query(Inventory)
	           .filter(visible_devices_clause(user_id))
	           .order_by(Inventory.label)
	           .all())
	_ = [d.security_profile for d in devices]
	_ = [d.var_mappings for d in devices]
	return devices


def can_edit_device(device: Inventory, user: User) -> bool:
	"""Owners edit their own devices; any admin may edit a global device."""
	return device.user_id == user.id or (
			device.is_global and user.role == "admin")


def same_endpoint_devices(db_session: Session, user_id: uuid.UUID, ip: str,
                          port: int | str,
                          exclude_id: uuid.UUID | None = None) -> list[Inventory]:
	"""Visible devices (own + global) already using this ip:port. Overlap is
	legitimate (NAT, VRFs, port-forwarded labs), so callers warn, never
	block; other users' private devices are never considered.

	:param exclude_id: the device being edited (it doesn't clash with itself)"""
	query = db_session.query(Inventory).filter(
		visible_devices_clause(user_id),
		Inventory.ip == ip, Inventory.port == int(port))
	if exclude_id is not None:
		query = query.filter(Inventory.id != exclude_id)
	return query.order_by(Inventory.label).all()


def same_endpoint_warning(devices: Sequence[Inventory], ip: str,
                          port: int | str) -> str | None:
	"""One warning naming the devices that share ip:port. Build it while the
	DB session is open (it reads labels); flash it after the success message.

	:returns: the warning; None when no device shares it"""
	if not devices:
		return None
	names = ", ".join(f"{d.label} (global)" if d.is_global else d.label
	                  for d in devices[:5]) + (", …" if len(devices) > 5 else "")
	return (f"{endpoint(ip, port)} is already used by {names}. That's fine for NAT, "
	        f"VRFs or port-forwarded labs, but they can't be in the same "
	        f"rollout.")


def partition_devices(devices: Sequence[Inventory]) -> tuple[list[Inventory], list[Inventory]]:
	"""Split visible devices into (global_devices, my_devices)."""
	global_devices = [d for d in devices if d.is_global]
	my_devices = [d for d in devices if not d.is_global]
	return global_devices, my_devices
