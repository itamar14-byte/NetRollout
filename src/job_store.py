"""Rollout job state in Redis — the one place that knows its key layout.

The orchestrator writes it, the web pages and the metrics read it; neither
spells a key. Jobs live only in the process that runs them, so whatever is
here when the app starts is left over from a crash and is cleared
(reset_stale()).

Keys: job:{id}:meta (hash: user_id, status, device_count, created_at,
started_at), user_jobs:{user_id} (set of job ids), the job queue (list), and
the pending / active counters (the Prometheus gauges).

redis-py types every reply as maybe-awaitable (one signature for its sync and
async clients); this client is synchronous, so each reply is cast to what it
is."""
import datetime
import uuid
from typing import TYPE_CHECKING, Any, cast

import redis

if TYPE_CHECKING:   # annotations only: the db package imports the web stack's models
	from src.db.connections import RedisConnection

QUEUE = "netrollout:job_queue"
PENDING = "netrollout:pending_count"
ACTIVE = "netrollout:active_count"


def _meta(job_id: uuid.UUID | str) -> str:
	""":returns: a job's hash key (`*` for a scan pattern)"""
	return f"job:{job_id}:meta"


def _user_jobs(user_id: uuid.UUID | str) -> str:
	""":returns: a user's set of job ids (`*` for a scan pattern)"""
	return f"user_jobs:{user_id}"


def _text(value: Any) -> str:
	""":returns: a Redis reply as text (bytes decoded)"""
	return value.decode() if isinstance(value, bytes) else str(value)


class JobStore:
	"""The rollout job keys in Redis (see the module's docstring)."""

	def __init__(self, redis_service: "RedisConnection") -> None:
		""":param redis_service: the app's Redis - the service, not its client: a
		 Redis switch replaces the client, and every call must use the current one"""
		self._redis = redis_service

	@property
	def _client(self) -> redis.Redis:
		return self._redis.client

	# ── written by the orchestrator ──

	def add(self, job_id: uuid.UUID, user_id: uuid.UUID,
	        device_count: int) -> None:
		"""A new job, waiting for a slot (not queued yet: enqueue())."""
		self._client.hset(_meta(job_id), mapping={
			"user_id": str(user_id), "status": "pending",
			"device_count": device_count,
			"created_at": datetime.datetime.now().isoformat()})
		self._client.sadd(_user_jobs(user_id), str(job_id))
		self._client.incr(PENDING)

	def enqueue(self, job_id: uuid.UUID) -> None:
		"""Queued: the dispatcher starts it when a slot is free."""
		self._client.rpush(QUEUE, str(job_id))

	def next_queued(self, timeout: int) -> str | None:
		"""The next queued entry (as stored — the caller checks it's a job
		id), or None after `timeout` seconds."""
		result = cast(tuple[bytes, bytes] | None, self._client.blpop(QUEUE, timeout=timeout))
		return None if result is None else _text(result[1])

	def unqueue(self, job_id: uuid.UUID) -> None:
		"""Out of the queue (a job cancelled before it started)."""
		self._client.lrem(QUEUE, 0, str(job_id))

	def set_status(self, job_id: uuid.UUID, status: str) -> None:
		""":param status: what Active Jobs shows (pending / active / cancelling)"""
		self._client.hset(_meta(job_id), "status", status)

	def started(self, job_id: uuid.UUID) -> None:
		"""Pending → active."""
		self._client.hset(_meta(job_id), mapping={
			"status": "active",
			"started_at": datetime.datetime.now().isoformat()})
		self._client.decr(PENDING)
		self._client.incr(ACTIVE)

	def finished(self, job_id: uuid.UUID, user_id: uuid.UUID,
	             was_running: bool) -> None:
		"""The job is over (its results are in Postgres): gone from here."""
		self._client.delete(_meta(job_id))
		self._client.srem(_user_jobs(user_id), str(job_id))
		self._client.decr(ACTIVE if was_running else PENDING)

	def reset_stale(self) -> int:
		"""At startup: no job of this process exists yet, so every job key is
		left over (a crash, a power loss — a clean stop drains first).

		:returns: how many jobs were cleared"""
		client = self._client
		metas = list(client.scan_iter(_meta("*")))
		for key in (*metas, *client.scan_iter(_user_jobs("*")),
		            QUEUE, PENDING, ACTIVE):
			client.delete(key)
		return len(metas)

	# ── read by the pages and the metrics ──

	def meta(self, job_id: uuid.UUID | str) -> dict[str, str]:
		""":returns: the job's fields (user_id, status, device_count, created_at,
		 started_at), as text; {} when it isn't running here"""
		fields = cast(dict[bytes, bytes], self._client.hgetall(_meta(job_id)))
		return {_text(k): _text(v) for k, v in fields.items()}

	def job_ids(self, user_id: uuid.UUID | str | None = None) -> list[str]:
		""":param user_id: whose; None: everyone's
		:returns: the jobs' ids"""
		if user_id is None:
			return [_text(k).split(":")[1]
			        for k in self._client.scan_iter(_meta("*"))]
		members = cast(set[bytes], self._client.smembers(_user_jobs(user_id)))
		return [_text(j) for j in members]

	def counts(self) -> tuple[int, int]:
		""":returns: (active, pending) - the gauges' values"""
		active: Any = self._client.get(ACTIVE)
		pending: Any = self._client.get(PENDING)
		return int(active or 0), int(pending or 0)
