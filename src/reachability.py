"""Is a device reachable from the NetRollout server?

One fast TCP connect to ip:port — the management endpoint a push would use,
from the server's point of view (the one that matters). Results are cached
briefly in Redis, so page loads stay cheap and "recent" means at most
CACHE_TTL seconds old.
"""
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterable, TypedDict, cast

import redis

from src.db.redis_db import REDIS_UNAVAILABLE

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
