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
from typing import Callable, Iterable

from src.db.redis_db import REDIS_UNAVAILABLE

PROBE_TIMEOUT = 2.0   # seconds; single attempt — this is a status hint
CACHE_TTL = 60        # seconds
MAX_PARALLEL = 32

Target = tuple[str, int]


def probe(ip: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
	try:
		with socket.create_connection((ip, int(port)), timeout=timeout):
			return True
	except (OSError, ValueError):
		return False


def _cache_key(target: Target) -> str:
	return f"reach:{target[0]}:{target[1]}"


class ReachabilityChecker:
	def __init__(self, redis_client: Callable, ttl: int | Callable = CACHE_TTL,
	             prober: Callable[[str, int], bool] = probe):
		# redis_client is a callable: the connection can be hot-swapped from
		# Server Management, so it's resolved on every use. ttl may be a
		# callable too (the System Setting), read each time results are cached
		self._redis = redis_client
		self._ttl = ttl
		self._probe = prober

	def check(self, targets: Iterable[Target], refresh: bool = False,
	          recheck_unreachable: bool = False) -> dict[Target, dict]:
		"""{(ip, port): {"reachable": bool, "checked_at": iso8601}}.
		:param refresh: ignore the cache and probe everything (Recheck)
		:param recheck_unreachable: trust cached "reachable" results but
		 re-probe cached failures — used before blocking a rollout, so a
		 device that just came back isn't blocked by a stale result
		"""
		targets = {(ip, int(port)) for ip, port in targets}
		results = {} if refresh else self._cached(targets)
		if recheck_unreachable:
			results = {t: r for t, r in results.items() if r["reachable"]}
		todo = sorted(t for t in targets if t not in results)
		if todo:
			now = datetime.now(timezone.utc).isoformat()
			with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(todo))) as ex:
				outcomes = ex.map(lambda t: self._probe(*t), todo)
				fresh = {t: {"reachable": ok, "checked_at": now}
				         for t, ok in zip(todo, outcomes)}
			self._store(fresh)
			results.update(fresh)
		return results

	def _cached(self, targets: set[Target]) -> dict[Target, dict]:
		if not targets:
			return {}
		ordered = sorted(targets)
		try:
			raw = self._redis().mget([_cache_key(t) for t in ordered])
		except REDIS_UNAVAILABLE:
			return {}  # no cache: everything gets probed
		return {t: json.loads(v) for t, v in zip(ordered, raw) if v}

	def _current_ttl(self) -> int:
		if not callable(self._ttl):
			return self._ttl
		try:
			return int(self._ttl())
		except Exception:   # settings unreachable: the default, not an error
			return CACHE_TTL

	def _store(self, results: dict[Target, dict]) -> None:
		ttl = self._current_ttl()
		try:
			pipe = self._redis().pipeline()
			for t, r in results.items():
				pipe.setex(_cache_key(t), ttl, json.dumps(r))
			pipe.execute()
		except REDIS_UNAVAILABLE:
			pass
