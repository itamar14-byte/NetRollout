"""The web app's rollouts: a job per rollout, queued in Redis and run in this
process, at most max_concurrent at a time (the System Setting) - their state
in Redis for the pages (JobStore), their results into Postgres when they
end. Stop / Restart drain them; a database move pauses them.

JobStore is the one place that knows the job state's key layout: the
orchestrator writes it, the web pages and the metrics read it; neither
spells a key. Jobs live only in the process that runs them, so whatever is
there when the app starts is left over from a crash and is cleared
(clear_stale_jobs -> JobStore.reset_stale()).

Keys: job:{id}:meta (hash: user_id, status, device_count, created_at,
started_at), user_jobs:{user_id} (set of job ids), the job queue (list), and
the pending / active counters (the Prometheus gauges); a job's live log
(src/rollout/log.py: live_log_keys) is the logger's, its leftovers cleared
here too.

`python -m src.jobs rollouts | stop-now [--clear]` (main()): the same keys read from
outside the app's process - netrollout stop / update and NetRollout Manager
list the rollouts before a stop and may have it cancel them at once.

redis-py types every reply as maybe-awaitable (one signature for its sync and
async clients); this client is synchronous, so each reply is cast to what it
is."""
import argparse
import datetime
import json
import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterator, Sequence
from typing import Any, cast, Callable

import redis
from sqlalchemy import ColumnElement, and_, func, not_
from sqlalchemy.exc import SQLAlchemyError

from src import runtime
from src.db.connections import (BackendServices, PostgresConnection, REDIS_UNAVAILABLE, RedisConnection,
                                load_config)
from src.db.tables import DeviceResult, JobMetadata, User
from src.rollout.engine import RolloutEngine, RolloutOptions, Device, DeviceResultDict
from src.rollout.log import RolloutLogger, live_log_keys


QUEUE = "netrollout:job_queue"
PENDING = "netrollout:pending_count"
ACTIVE = "netrollout:active_count"
# set from outside the app (netrollout stop / update, the Manager): the stop
# under way cancels the running rollouts now instead of waiting for them
STOP_NOW = "netrollout:stop_now"
STOP_NOW_SECONDS = 15 * 60   # an update's download, backup and restart fit in it


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
		"""The status of a job still here - never one that has ended (a cancel
		racing the job's end would bring its hash back as a phantom row).

		:param status: what Active Jobs shows (pending / active / cancelling)"""
		self._client.eval(_SET_IF_EXISTS, 1, _meta(job_id), "status", status)

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
		self.forget(job_id, user_id)
		self._client.decr(ACTIVE if was_running else PENDING)

	def forget(self, job_id: uuid.UUID, user_id: uuid.UUID) -> None:
		"""A job's hash and listing gone, the counters untouched (a job whose
		add() didn't complete was never counted)."""
		self._client.delete(_meta(job_id))
		self._client.srem(_user_jobs(user_id), str(job_id))

	def reset_stale(self) -> int:
		"""At startup: no job of this process exists yet, so every job key is
		left over (a crash, a power loss — a clean stop drains first).

		:returns: how many jobs were cleared"""
		client = self._client
		metas = list(client.scan_iter(_meta("*")))
		histories = client.scan_iter(live_log_keys("*")[0])
		for key in (*metas, *client.scan_iter(_user_jobs("*")), *histories,
		            QUEUE, PENDING, ACTIVE, STOP_NOW):
			client.delete(key)
		return len(metas)

	def request_stop_now(self) -> None:
		"""Ask the stop / restart under way to cancel the running rollouts now
		(netrollout stop / update, the Manager); the next start clears it, the
		scripts clear it when their stop or update doesn't happen
		(clear_stop_now), and it expires after STOP_NOW_SECONDS - else a later
		stop that chose to wait would cancel at once."""
		self._client.set(STOP_NOW, "1", ex=STOP_NOW_SECONDS)

	def clear_stop_now(self) -> None:
		"""The stop or update that asked for a stop now isn't happening: a
		later stop waits for the rollouts again."""
		self._client.delete(STOP_NOW)

	def stop_now_requested(self) -> bool:
		""":returns: whether a stop now was asked for (False when Redis can't say)"""
		try:
			return bool(self._client.exists(STOP_NOW))
		except REDIS_UNAVAILABLE:
			return False

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

	def rollouts(self) -> list[dict[str, Any]]:
		"""Every job here, oldest first, in RolloutOrchestrator.jobs()' shape -
		for a process that isn't the one running them (netrollout stop /
		update, the Manager: python -m src.jobs rollouts).

		:returns: [{job_id, user_id, devices, state (queued / running /
		 cancelling), started (to the second, or None)}]"""
		rows: list[tuple[str, dict[str, Any]]] = []
		for job_id in self.job_ids():
			meta = self.meta(job_id)
			if not meta:
				continue              # ended between the scan and the read
			started = meta.get("started_at")
			rows.append((meta.get("created_at", ""), {
				"job_id": job_id, "user_id": meta.get("user_id", ""),
				"devices": int(meta.get("device_count") or 0),
				"state": _STATES.get(meta.get("status", ""), "running"),
				"started": started[:19] if started else None}))
		return [row for _, row in sorted(rows, key=lambda r: r[0])]


# a job's status in Redis -> what the waiting lists call it
_STATES = {"pending": "queued", "active": "running", "cancelling": "cancelling"}

# HSET only on a hash that exists (set_status), atomically
_SET_IF_EXISTS = ("if redis.call('exists', KEYS[1]) == 1 then "
                  "return redis.call('hset', KEYS[1], ARGV[1], ARGV[2]) end return 0")


# Dispatcher retry backoff while Redis is down (seconds)
_BACKOFF_START, _BACKOFF_MAX = 1, 30
# Finite BLPOP timeout so the loop re-reads backend.redis.client
# regularly and picks up a hot-swapped connection
_BLPOP_TIMEOUT = 5
# Drain: how long cancelled jobs get to record their results, and how often
# the wait for running jobs is reported
_CANCEL_WAIT = 60
_DRAIN_REPORT_EVERY = 30
# Saving a finished job's results: tries, and the wait before each retry
_SAVE_TRIES, _SAVE_RETRY_WAIT = 3, 2
# A stop waits this long at most for results still being saved
_SAVE_WAIT = 60
QUEUED_CANCEL_REASON = ("Cancelled: NetRollout restarted before this rollout "
                        "started. Nothing was sent to the devices.")
CANCELLED_BEFORE_START = ("Cancelled before it started. Nothing was sent to "
                          "the devices.")
# what the pages and a queued job's live log say while it waits
QUEUED_LINE = "Queued — waiting for a free slot"


DRAINING_MESSAGE = ("NetRollout is stopping or restarting — new rollouts are "
                    "paused. Try again once it's back.")


PAUSED_MESSAGE = ("NetRollout is moving to another database — new rollouts "
                  "are paused. Try again once the move is done.")


class Draining(Exception):
	"""submit() while NetRollout is stopping or restarting. Its text is the
	message for people."""
	def __init__(self, message: str = DRAINING_MESSAGE):
		super().__init__(message)


class Paused(Draining):
	"""submit() while rollouts are paused for a database move (pause())."""
	def __init__(self) -> None:
		super().__init__(PAUSED_MESSAGE)


class RolloutJob:
	"""One rollout: its engine, its log, its thread, its results."""

	def __init__(self, job_id: uuid.UUID, user_id: uuid.UUID,
	             engine: RolloutEngine, options: RolloutOptions,
	             redis_client: redis.Redis | None = None) -> None:
		""":param user_id: who started it
		:param engine: what it runs (devices, commands, options)
		:param options: how it logs (web page / console, verbose)
		:param redis_client: where its live log is (the web app's)"""
		self.job_id = job_id
		self.user_id = user_id
		self.started_at: datetime.datetime | None = None
		self.results: list[DeviceResultDict] = []
		self._engine = engine
		self._logger = RolloutLogger(options.webapp, options.verbose,
		                             job_id=str(job_id), prefix="rollout",
		                             redis_client=redis_client)
		self._cancel_flag = threading.Event()
		self._thread: threading.Thread | None = None

	def start(self, on_complete: Callable[[uuid.UUID], None]) -> None:
		"""Run the rollout in its own thread.

		:param on_complete: called with the job's id when it ends, however -
		 it releases the orchestrator's slot"""
		self.started_at = datetime.datetime.now()

		def _engine_run() -> None:
			# on_complete must always fire — it releases the orchestrator slot.
			# An escaped exception here would leak the slot permanently.
			try:
				self.results = self._engine.run(self._cancel_flag, self._logger)
			except Exception as e:
				self._logger.notify(f"Rollout aborted: {e}", "red")
			finally:
				on_complete(self.job_id)

		thread = threading.Thread(target=_engine_run, daemon=True)
		self._thread = thread
		thread.start()

	def cancel(self) -> None:
		"""Ask the running rollout to stop: devices not reached yet are
		skipped, one being configured finishes."""
		self._cancel_flag.set()

	def cancel_before_start(self, reason: str) -> None:
		"""A queued job that will never run: every device recorded as
		cancelled, with the reason in its log, so it shows in Results."""
		self.started_at = datetime.datetime.now()
		self._logger.notify(reason, "red", important=True)
		self.results = self._engine.cancelled_results()

	def is_over(self) -> bool:
		""":returns: whether it ran and has ended (a queued job isn't)"""
		return self._thread is not None and not self._thread.is_alive()

	def follow_log(self, over: Callable[[], bool]) -> Iterator[str | None]:
		"""Its live log for the page's stream (RolloutLogger.follow).

		:param over: whether it has ended, asked while nothing comes"""
		return self._logger.follow(over)

	def log_cleanup(self) -> None:
		"""End its live log (readers get "done"; the keys go)."""
		return self._logger.redis_cleanup()

	def get_device_count(self) -> int:
		""":returns: how many devices it targets"""
		return self._engine.device_count


class RolloutOrchestrator:
	"""The web app's rollouts: submit, cancel, drain (stop / restart), pause
	(a database move), and the dispatcher that starts queued ones."""

	def __init__(self, backend_obj: BackendServices, max_concurrent: int = 4)\
			->	None:
		""":param backend_obj: Postgres (results, job metadata) and Redis (state,
		 queue, live logs)
		:param max_concurrent: rollouts running at the same time; more wait"""
		self.max_concurrent = max_concurrent
		self._backend = backend_obj
		self._store = JobStore(backend_obj.redis)
		self._slots = threading.Semaphore(max_concurrent)
		self._jobs: dict[uuid.UUID, RolloutJob] = {}
		self._lock = threading.Lock()
		self._draining = False
		self._paused = False
		self._saving = 0     # finished jobs whose results are being written
		threading.Thread(target=self._dispatcher, daemon=True).start()

	@property
	def draining(self) -> bool:
		"""True once a stop / restart began: new rollouts are refused."""
		return self._draining

	@property
	def paused(self) -> bool:
		"""True while pause()d: new rollouts are refused, the queued and running
		ones go on."""
		return self._paused

	def pause(self) -> None:
		"""Refuse new rollouts (Paused) and let the queued and running ones
		finish - unlike drain(), nothing is cancelled and resume() undoes it.
		For a database move, which waits until counts() is all zero."""
		with self._lock:
			self._paused = True

	def idle(self) -> bool:
		"""Nothing queued, running, or still writing its results - nothing of
		the rollouts will write to the database any more (while pause()d)."""
		with self._lock:
			return not self._jobs and not self._saving

	def resume(self) -> None:
		"""New rollouts accepted again - unless a stop / restart began
		meanwhile: a drain is never undone."""
		with self._lock:
			self._paused = False

	def counts(self) -> dict[str, int]:
		"""Rollouts of this process, from memory (so it works while Redis is
		down): running (started) and queued (waiting for a slot)."""
		with self._lock:
			running = sum(1 for j in self._jobs.values()
			              if j.started_at is not None)
			return {"running": running, "queued": len(self._jobs) - running}

	def submit(self, devices: list[Device], commands: list[str], params:
	RolloutOptions, user_id: uuid.UUID,
	           comment: str | None = None) -> uuid.UUID:
		"""Queue a rollout; it starts when a slot is free.

		:param devices: the targets, credentials resolved
		:param commands: what's pushed, in order (variables resolved per device)
		:param params: verify, verbose, parallelism
		:param user_id: who starts it
		:param comment: the job's note (Results, Active Jobs)
		:returns: the new job's id
		:raises Draining: NetRollout is stopping or restarting
		:raises Paused: rollouts are paused for a database move"""
		self._refuse_if_closed()   # before RolloutJob: it creates the log file
		engine = RolloutEngine(params, devices, commands)
		job = RolloutJob(uuid.uuid4(), user_id, engine, params,
		                 redis_client=self._backend.redis.client)

		with self._lock:
			self._refuse_if_closed()
			self._jobs[job.job_id] = job

		added = False
		try:
			self._store.add(job.job_id, user_id, job.get_device_count())
			added = True
			with self._backend.postgres.get_session() as db_session:
				db_session.add(JobMetadata(job_id=job.job_id,
				                           user_id=user_id,
				                           commands=commands,
				                           comment=comment))
			self._store.enqueue(job.job_id)
		except BaseException:
			self._withdraw(job, added)
			raise
		return job.job_id

	def _withdraw(self, job: RolloutJob, added: bool) -> None:
		"""Undo a submit that failed partway: no job left behind here or in
		Redis (best-effort - Redis may be why it failed; the next start clears
		what stays).

		:param added: whether JobStore.add completed (it counted the job)"""
		with self._lock:
			self._jobs.pop(job.job_id, None)
		def gone() -> None:
			if added:
				self._store.finished(job.job_id, job.user_id, was_running=False)
			else:
				self._store.forget(job.job_id, job.user_id)

		for undo in (lambda: self._store.unqueue(job.job_id), gone):
			try:
				undo()
			except Exception:     # noqa: BLE001 - the submit's own error is what's raised
				pass

	def refusal(self) -> str | None:
		"""Why a new rollout would be refused now (for people), or None."""
		if self._draining:
			return DRAINING_MESSAGE
		return PAUSED_MESSAGE if self._paused else None

	def _refuse_if_closed(self) -> None:
		""":raises Draining: NetRollout is stopping or restarting
		:raises Paused: rollouts are paused for a database move"""
		if self._draining:
			raise Draining()
		if self._paused:
			raise Paused()

	def cancel(self, job_id: uuid.UUID) -> None:
		"""Cancel a rollout of this process (unknown: nothing). A queued one
		ends at once - recorded as cancelled, never started; a running one is
		asked to stop (devices not reached yet are skipped)."""
		with self._lock:
			job = self._jobs.get(job_id, None)
			queued = job is not None and self._claim(job)
		if job is None:
			return
		if queued:
			self._cancel_queued(job, CANCELLED_BEFORE_START)
			return
		job.cancel()
		self._store.set_status(job.job_id, "cancelling")

	@staticmethod
	def _claim(job: RolloutJob) -> bool:
		"""Under the lock: take a queued job for whoever calls (the dispatcher
		to start it, a cancel or the drain to record it) - each job is claimed
		once (started_at set).

		:returns: whether it was queued (now claimed by the caller)"""
		if job.started_at is not None:
			return False
		job.started_at = datetime.datetime.now()
		return True

	def jobs(self) -> list[dict[str, Any]]:
		"""This process's rollouts (a database move lists what it waits for)."""
		with self._lock:
			return [{"job_id": j.job_id, "user_id": j.user_id,
			         "devices": j.get_device_count(),
			         "state": "running" if j.started_at is not None else "queued",
			         "started": j.started_at.isoformat(timespec="seconds") if j.started_at else None}
			        for j in self._jobs.values()]

	def get_job(self, job_id: uuid.UUID) -> RolloutJob | None:
		""":returns: this process's job (None: not here - over, or another's)"""
		with self._lock:
			job = self._jobs.get(job_id, None)
		return job

	def _dispatcher(self) -> None:
		"""Start queued jobs as slots free up - forever, from its own thread.
		It must never die: if it exits, no job starts again until the process
		restarts. Redis outages are waited out with backoff instead."""
		backoff = _BACKOFF_START
		while True:
			try:
				entry = self._store.next_queued(_BLPOP_TIMEOUT)
			except REDIS_UNAVAILABLE as e:
				print(f"[NetRollout] dispatcher: Redis unavailable ({e}); "
				      f"retrying in {backoff}s", flush=True)
				time.sleep(backoff)
				backoff = min(backoff * 2, _BACKOFF_MAX)
				continue
			except Exception as e:   # noqa: BLE001 - this thread must not die
				# e.g. a Redis switch (Server Management) closes the client this
				# wait is blocked on: "I/O operation on closed file" - the next
				# round reads the new client
				print(f"[NetRollout] dispatcher: reading the queue failed ({e!r}); "
				      f"retrying in {backoff}s", flush=True)
				time.sleep(backoff)
				backoff = min(backoff * 2, _BACKOFF_MAX)
				continue
			backoff = _BACKOFF_START
			if entry is None:
				continue
			try:
				job_id = uuid.UUID(entry)
			except ValueError:
				print(f"[NetRollout] dispatcher: skipping malformed queue "
				      f"entry {entry!r}", flush=True)
				continue

			self._slots.acquire()

			# Claimed under the lock (_claim), started outside it: drain() and
			# cancel() claim a queued job under the lock too, so each job is
			# either run here or recorded as cancelled there. start() isn't
			# called under the lock — a job that ends at once needs it for its
			# cleanup.
			with self._lock:
				job = self._jobs.get(job_id)
				if job is None or self._draining or not self._claim(job):
					job = None
			if job is None:
				self._slots.release()   # gone, or a cancel / drain() records it
				continue

			# Before start(): a job that ends at once has its hash deleted, and
			# this would bring it back. A failure only leaves status / counters
			# stale - the job starts anyway, and the loop must not die.
			try:
				self._store.started(job.job_id)
			except Exception as e:   # noqa: BLE001
				print(f"[NetRollout] dispatcher: job {job.job_id} starts but its "
				      f"status update failed ({e!r})", flush=True)
			job.start(self._cleanup)

	def _cleanup(self, job_id: uuid.UUID) -> None:
		"""A job's end (its on_complete): finalize, then free its slot."""
		try:
			self._finalize(job_id)
		finally:
			self._slots.release()

	def drain(self, deadline: float, report: Callable[[str], None] = print) -> None:
		"""Stop for a restart or shutdown. New rollouts are refused (submit
		raises Draining); queued ones are recorded as cancelled; running ones
		may finish for `deadline` seconds and are then cancelled — a cancel
		stops devices that haven't connected yet, and the job still records
		its results; a stop-now request (JobStore.request_stop_now: netrollout
		stop / update, the Manager) cancels them at once. Returns when no job
		is left (or _CANCEL_WAIT after the cancel) and no finished job is still
		writing its results (at most _SAVE_WAIT more): the exit that follows
		must not cut a save.

		:param deadline: seconds running rollouts may take to finish
		:param report: where progress lines go (the console by default)"""
		with self._lock:
			self._draining = True
			queued = [j for j in self._jobs.values() if self._claim(j)]
		for job in queued:
			self._cancel_queued(job, QUEUED_CANCEL_REASON)
		if queued:
			report(f"[NetRollout] {len(queued)} queued rollout(s) cancelled — "
			       f"they hadn't started")

		end = time.monotonic() + deadline
		next_report = time.monotonic()
		while (running := self.counts()["running"]) and \
				time.monotonic() < end and not self._store.stop_now_requested():
			if time.monotonic() >= next_report:
				report(f"[NetRollout] Waiting for {running} running rollout(s) "
				       f"to finish before stopping (at most "
				       f"{int(end - time.monotonic())}s more)")
				next_report = time.monotonic() + _DRAIN_REPORT_EVERY
			time.sleep(0.5)

		if running := self.counts()["running"]:
			report(f"[NetRollout] Cancelling {running} running rollout(s) — "
			       f"devices already being configured get up to "
			       f"{_CANCEL_WAIT}s to finish")
			with self._lock:
				jobs = list(self._jobs.values())
			for job in jobs:
				job.cancel()
				try:
					self._store.set_status(job.job_id, "cancelling")
				except REDIS_UNAVAILABLE:
					pass
			end = time.monotonic() + _CANCEL_WAIT
			while self.counts()["running"] and time.monotonic() < end:
				time.sleep(0.5)

		# A job that just ended has left the job table but may still be
		# writing its results: the exit after the drain would cut that off
		end = time.monotonic() + _SAVE_WAIT
		while self._saving_now() and time.monotonic() < end:
			time.sleep(0.2)
		if self._saving_now():
			report(f"[NetRollout] A rollout's results are still being saved after "
			       f"{_SAVE_WAIT}s - stopping anyway: they may be lost")

	def _saving_now(self) -> bool:
		""":returns: whether a finished job is still writing its results"""
		with self._lock:
			return self._saving > 0

	def _cancel_queued(self, job: RolloutJob, reason: str) -> None:
		"""Record a queued job (claimed by the caller) as cancelled - a cancel,
		or a drain: its definition (devices, commands) lives only in this
		process, so after a restart it could never run: recorded, not lost.

		:param reason: why, in its log"""
		try:
			job.cancel_before_start(reason)
			try:
				self._store.unqueue(job.job_id)
			except REDIS_UNAVAILABLE:
				pass   # a leftover entry is cleared at the next start
			self._finalize(job.job_id, was_running=False)
		except Exception as e:
			print(f"[NetRollout] Couldn't record queued rollout {job.job_id} "
			      f"as cancelled ({e})", flush=True)
			with self._lock:   # else the drain would wait for it as "running"
				self._jobs.pop(job.job_id, None)

	def _finalize(self, job_id: uuid.UUID, was_running: bool = True) -> None:
		"""A job is over: its results to Postgres, its keys out of Redis, its
		live log closed. Each step runs even if another failed — a service
		down at that moment must not lose the results (they go to a file
		then) nor leave the job "active" forever."""
		with self._lock:
			job = self._jobs.pop(job_id, None)
			if job:
				self._saving += 1      # idle() waits for the results
		if not job:
			return
		try:
			self._save_results(job)
		finally:
			with self._lock:
				self._saving -= 1
			try:
				self._store.finished(job.job_id, job.user_id, was_running)
			except REDIS_UNAVAILABLE as e:
				print(f"[NetRollout] rollout {job.job_id}: Redis unavailable, "
				      f"its live status stays until the next start ({e})",
				      flush=True)
			try:
				job.log_cleanup()      # live log viewers get "done"
			except REDIS_UNAVAILABLE:
				pass

	def _save_results(self, job: "RolloutJob") -> None:
		"""Into Postgres, retried; if that keeps failing, into a JSON file in
		logs/ (ACTION NEEDED on the console) — never dropped."""
		completed_at = datetime.datetime.now()
		error = None
		for attempt in range(_SAVE_TRIES):
			if attempt:
				time.sleep(_SAVE_RETRY_WAIT)
			try:
				with self._backend.postgres.get_session() as db_session:
					for result in job.results:
						db_session.add(DeviceResult(
							**result, user_id=job.user_id, job_id=job.job_id,
							started_at=job.started_at, completed_at=completed_at))
				return
			except Exception as e:     # noqa: BLE001 — any failure: keep them
				error = e
		path = runtime.logs_dir() / f"unsaved-results-{job.job_id}.json"
		try:   # the last resort: nothing here may raise
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_text(json.dumps({
				"job_id": job.job_id, "user_id": job.user_id,
				"started_at": job.started_at, "completed_at": completed_at,
				"results": job.results}, indent=1, default=str), encoding="utf-8")
			where = f"saved to {path}"
		except Exception as e:     # noqa: BLE001
			where = f"and couldn't be written to {path} either ({e})"
		print(f"[NetRollout] ACTION NEEDED — rollout {job.job_id}: its results "
		      f"couldn't be saved to the database ({error}); {where}",
		      flush=True)


def job_status(rows: Sequence[DeviceResult]) -> str:
	""":returns: a job's status from its devices': cancelled if any was; failed
	 if all failed; partial if any failed or was partial; else success"""
	statuses = {r.status for r in rows}
	if "cancelled" in statuses:
		return "cancelled"
	if all(r.status == "failed" for r in rows):
		return "failed"
	if any(r.status in ("failed", "partial") for r in rows):
		return "partial"
	return "success"


# what job_status can say, in the Results page's filter order
JOB_STATUSES = ("success", "partial", "failed", "cancelled")


def job_status_condition(status: str) -> ColumnElement[bool]:
	"""job_status' rules in SQL, over one job's device results (a HAVING
	condition of a query grouped by job_id): true exactly for the jobs
	job_status calls status.

	:param status: one of JOB_STATUSES
	:raises ValueError: any other status"""
	def any_device(*statuses: str) -> ColumnElement[bool]:
		return func.bool_or(DeviceResult.status.in_(statuses))

	all_failed = func.bool_and(DeviceResult.status == "failed")
	if status == "cancelled":
		return any_device("cancelled")
	if status == "failed":
		return and_(not_(any_device("cancelled")), all_failed)
	if status == "partial":
		return and_(not_(any_device("cancelled")), not_(all_failed),
		            any_device("failed", "partial"))
	if status == "success":
		return not_(any_device("cancelled", "failed", "partial"))
	raise ValueError(f"no job status {status!r}")


def with_owners(rollouts: Sequence[dict[str, Any]],
                postgres: PostgresConnection) -> list[dict[str, Any]]:
	"""The waiting lists' rows (a database move, the Restart dialog,
	netrollout stop / update): each rollout with its owner's username, in one
	query.

	:param rollouts: RolloutOrchestrator.jobs() or JobStore.rollouts() - their
	 user_id a UUID or its text
	:param postgres: where the users are
	:returns: the rollouts, job_id and user_id as text, plus "user" ("?" for
	 an id without a user)"""
	ids = set()
	for rollout in rollouts:
		try:
			ids.add(uuid.UUID(str(rollout["user_id"])))
		except ValueError:
			pass
	names: dict[str, str] = {}
	if ids:
		with postgres.get_session() as session:
			names = {str(row.id): row.username for row in
			         session.query(User.id, User.username).filter(User.id.in_(ids))}
	return [{**r, "job_id": str(r["job_id"]), "user_id": str(r["user_id"]),
	         "user": names.get(str(r["user_id"]), "?")} for r in rollouts]


def rollouts_text(rollouts: Sequence[dict[str, Any]]) -> str:
	"""with_owners' rows for a terminal: a heading line and a table, each line
	indented as the scripts' messages are; "" when there are none."""
	if not rollouts:
		return ""
	n = len(rollouts)
	lines = [f"{n} rollout{'s' if n != 1 else ''} running or queued:",
	         f"   {'By':<20} {'Devices':>7}  {'State':<10}  Started"]
	for r in rollouts:
		started = (r["started"] or "-").replace("T", " ")
		lines.append(f"   {str(r['user'])[:20]:<20} {r['devices']:>7}  "
		             f"{r['state']:<10}  {started}")
	return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
	"""python -m src.jobs rollouts [--json] | stop-now [--clear] - run in the app's
	container (docker compose exec app) by netrollout stop / update and
	NetRollout Manager: the running app's rollouts, read from Redis (its
	JobStore keys, not the orchestrator, which lives in the app's process),
	and the request that its stop cancel them at once
	(JobStore.request_stop_now). The connections are resolved as the app's:
	config/runtime.env over the environment.

	:param argv: the arguments; sys.argv's when None
	:returns: the exit code: 0 done, 1 Redis unreachable (why, on stderr)"""
	parser = argparse.ArgumentParser(prog="python -m src.jobs")
	sub = parser.add_subparsers(dest="command", required=True)
	listing = sub.add_parser("rollouts", help="the rollouts running or queued "
	                                          "(nothing printed when none)")
	listing.add_argument("--json", action="store_true",
	                     help='one line: {"rollouts": [...]}')
	stop_now = sub.add_parser("stop-now", help="the stop under way cancels the "
	                                           "running rollouts at once")
	stop_now.add_argument("--clear", action="store_true",
	                      help="it isn't happening: a later stop waits for them")
	args = parser.parse_args(argv)
	load_config(runtime.runtime_env())
	redis_conn = RedisConnection()
	try:
		store = JobStore(redis_conn)
		if args.command == "stop-now" and args.clear:
			store.clear_stop_now()
			print("A later stop lets the running rollouts finish first.")
			return 0
		if args.command == "stop-now":
			store.request_stop_now()
			print("The running rollouts are cancelled as NetRollout stops - "
			      "devices being configured finish first, and every result is "
			      "recorded.")
			return 0
		rows = store.rollouts()
	except REDIS_UNAVAILABLE as e:
		print(f"Redis isn't reachable ({e})", file=sys.stderr)
		return 1
	finally:
		redis_conn.disconnect()
	postgres = PostgresConnection()
	try:
		named = with_owners(rows, postgres)
	except SQLAlchemyError:       # the names only: the list stands without them
		named = [{**r, "user": "?"} for r in rows]
	finally:
		postgres.engine.dispose()
	if args.json:
		print(json.dumps({"rollouts": named}))
	else:
		print(rollouts_text(named), end="")
	return 0


def clear_stale_jobs(redis_conn: RedisConnection) -> None:
	"""Rollout jobs live only in the process running them: any job state in
	Redis at startup is left over from a crash and would show as a job that
	never ends (and skew the metrics). Never stops the start."""
	try:
		cleared = JobStore(redis_conn).reset_stale()
	except REDIS_UNAVAILABLE as e:
		print(f"[NetRollout] Leftover rollout state not cleared: Redis "
		      f"unavailable ({e})", flush=True)
		return
	if cleared:
		print(f"[NetRollout] Cleared {cleared} rollout(s) left over from a "
		      f"previous run that didn't stop cleanly", flush=True)


def build_kpi(results_30d: Sequence[DeviceResult],
              label_map: dict[str, str]) -> dict[str, Any]:
	"""The dashboard's tiles from the last 30 days' device results.

	:param label_map: device IP → its label, to name the most-failed device
	:returns: success_rate (%, None without results), jobs_30d,
	 device_pushes (device results: a device pushed to in a rollout, failed
	 ones too - not distinct devices), commands_pushed, top_failed ({ip,
	 label, fail_count} or None)"""
	total_ops = len(results_30d)
	jobs_30d = len({r.job_id for r in results_30d})
	success_count = sum(1 for r in results_30d if r.status == "success")

	fail_counts_ip: dict[str, int] = defaultdict(int)
	for r in results_30d:
		if r.status == "failed":
			fail_counts_ip[r.device_ip] += 1
	top_failed = None
	if fail_counts_ip:
		top_ip = max(fail_counts_ip, key=lambda ip: fail_counts_ip[ip])
		top_failed = {"ip": top_ip, "label": label_map.get(top_ip),
		              "fail_count": fail_counts_ip[top_ip]}

	return {
		"success_rate": round(
			success_count / total_ops * 100) if total_ops else None,
		"jobs_30d": jobs_30d,
		"device_pushes": total_ops,
		"commands_pushed": sum(r.commands_sent for r in results_30d),
		"top_failed": top_failed
	}


if __name__ == "__main__":
	sys.exit(main())
