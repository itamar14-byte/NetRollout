import datetime
import json
import threading
import time
import uuid
from typing import Callable

from redis.client import PubSub

from src import runtime
from src.core import RolloutEngine, RolloutOptions, Device, DeviceResultDict
from src.db.tables import DeviceResult, JobMetadata
from src.job_store import JobStore
from src.logging_utils import RolloutLogger
from src.db.backend import BackendServices
from src.db.redis_db import REDIS_UNAVAILABLE

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
QUEUED_CANCEL_REASON = ("Cancelled: NetRollout restarted before this rollout "
                        "started. Nothing was sent to the devices.")


DRAINING_MESSAGE = ("NetRollout is stopping or restarting — new rollouts are "
                    "paused. Try again once it's back.")


class Draining(Exception):
	"""submit() while NetRollout is stopping or restarting."""
	pass


class RolloutJob:
	def __init__(self, job_id: uuid.UUID, user_id: uuid.UUID,
	             engine: RolloutEngine, options: RolloutOptions,
	             redis_client=None) -> None:
		self.job_id = job_id
		self.user_id = user_id
		self.started_at: datetime.datetime | None = None
		self.results: list[DeviceResultDict] = []
		self._engine = engine
		self._logger = RolloutLogger(options.webapp, options.verbose,
		                             job_id=str(job_id), prefix="rollout",
		                             redis_client=redis_client)
		self._cancel_flag = threading.Event()
		self._thread = None

	def start(self, on_complete: Callable[[uuid.UUID], None]) -> None:
		self.started_at = datetime.datetime.now()

		def _engine_run():
			# on_complete must always fire — it releases the orchestrator slot.
			# An escaped exception here would leak the slot permanently.
			try:
				self.results = self._engine.run(self._cancel_flag, self._logger)
			except Exception as e:
				self._logger.notify(f"Rollout aborted: {e}", "red")
			finally:
				on_complete(self.job_id)

		self._thread = threading.Thread(target=_engine_run, daemon=True)
		self._thread.start()

	def cancel(self) -> None:
		self._cancel_flag.set()

	def cancel_before_start(self, reason: str) -> None:
		"""A queued job that will never run: every device recorded as
		cancelled, with the reason in its log, so it shows in Results."""
		self.started_at = datetime.datetime.now()
		self._logger.notify(reason, "red", important=True)
		self.results = self._engine.cancelled_results()

	def is_alive(self) -> bool:
		return self._thread is not None and self._thread.is_alive()

	def get_log_queue(self) -> PubSub:
		return self._logger.subscribe()

	def get_log_history(self) -> list[str]:
		return self._logger.get_history()

	def log_cleanup(self) -> None:
		return self._logger.redis_cleanup()

	def get_device_count(self) -> int:
		return self._engine.device_count


class RolloutOrchestrator:
	def __init__(self, backend_obj: BackendServices, max_concurrent: int = 4)\
			->	None:
		self.max_concurrent = max_concurrent
		self._backend = backend_obj
		self._store = JobStore(backend_obj.redis)
		self._slots = threading.Semaphore(max_concurrent)
		self._jobs: dict[uuid.UUID, RolloutJob] = {}
		self._lock = threading.Lock()
		self._draining = False
		threading.Thread(target=self._dispatcher, daemon=True).start()

	@property
	def draining(self) -> bool:
		"""True once a stop / restart began: new rollouts are refused."""
		return self._draining

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
		""":raises Draining: NetRollout is stopping or restarting"""
		if self._draining:   # before RolloutJob: it creates the log file
			raise Draining()
		engine = RolloutEngine(params, devices, commands)
		job = RolloutJob(uuid.uuid4(), user_id, engine, params,
		                 redis_client=self._backend.redis.client)

		with self._lock:
			if self._draining:
				raise Draining()
			self._jobs[job.job_id] = job

		self._store.add(job.job_id, user_id, job.get_device_count())

		with self._backend.postgres.get_session() as db_session:
			db_session.add(JobMetadata(job_id=job.job_id,
			                           user_id=user_id,
			                           commands=commands,
			                           comment=comment))

		self._store.enqueue(job.job_id)
		return job.job_id

	def cancel(self, job_id: uuid.UUID) -> None:
		with self._lock:
			job = self._jobs.get(job_id, None)
		if job:
			job.cancel()
			self._store.set_status(job.job_id, "cancelling")

	def get_job(self, job_id: uuid.UUID) -> RolloutJob | None:
		with self._lock:
			job = self._jobs.get(job_id, None)
		return job

	def _dispatcher(self) -> None:
		# Must never die: if this thread exits, no job ever starts again
		# until the process restarts. Redis outages are waited out with
		# backoff instead.
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

			# Claimed under the lock (started_at set), started outside it: drain()
			# reads "queued" (not started) under the lock too, so each job is
			# either run here or cancelled there. start() isn't called under
			# the lock — a job that ends at once needs it for its cleanup.
			with self._lock:
				job = self._jobs.get(job_id)
				claimed = job is not None and not self._draining
				if claimed:
					job.started_at = datetime.datetime.now()
			if not claimed:
				self._slots.release()   # gone, or drain() records it
				continue
			job.start(self._cleanup)

			# The job is already running; a Redis failure here only leaves
			# status/counters stale, so it must not take the loop down.
			try:
				self._store.started(job.job_id)
			except REDIS_UNAVAILABLE as e:
				print(f"[NetRollout] dispatcher: job {job.job_id} started but "
				      f"status update failed ({e})", flush=True)

	def _cleanup(self, job_id: uuid.UUID) -> None:
		try:
			self._finalize(job_id)
		finally:
			self._slots.release()

	def drain(self, deadline: float, report=print) -> None:
		"""Stop for a restart or shutdown. New rollouts are refused (submit
		raises Draining); queued ones are recorded as cancelled; running ones
		may finish for `deadline` seconds and are then cancelled — a cancel
		stops devices that haven't connected yet, and the job still records
		its results. Returns when no job is left, or _CANCEL_WAIT after the
		cancel. report: where progress lines go (the console by default)."""
		with self._lock:
			self._draining = True
			queued = [j for j in self._jobs.values() if j.started_at is None]
		for job in queued:
			self._cancel_queued(job)
		if queued:
			report(f"[NetRollout] {len(queued)} queued rollout(s) cancelled — "
			       f"they hadn't started")

		end = time.monotonic() + deadline
		next_report = time.monotonic()
		while (running := self.counts()["running"]) and \
				time.monotonic() < end:
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

	def _cancel_queued(self, job: RolloutJob) -> None:
		# Its definition (devices, commands) lives only in this process, so
		# after a restart it could never run: record it instead of losing it
		try:
			job.cancel_before_start(QUEUED_CANCEL_REASON)
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
		if not job:
			return
		try:
			self._save_results(job)
		finally:
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
