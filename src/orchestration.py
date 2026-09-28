import datetime
import threading
import time
import uuid
from typing import Callable

from redis.client import PubSub

from src.core import RolloutEngine, RolloutOptions, Device, DeviceResultDict
from src.db.tables import DeviceResult, JobMetadata
from src.logging_utils import RolloutLogger
from src.db.backend import BackendServices
from src.db.redis_db import REDIS_UNAVAILABLE

# Dispatcher retry backoff while Redis is down (seconds)
_BACKOFF_START, _BACKOFF_MAX = 1, 30
# Finite BLPOP timeout so the loop re-reads backend.redis.client
# regularly and picks up a hot-swapped connection
_BLPOP_TIMEOUT = 5


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

	def is_alive(self) -> bool:
		return self._thread is not None and self._thread.is_alive()

	def get_log_queue(self) -> PubSub:
		return self._logger.subscribe()

	def get_log_history(self) -> list[str]:
		return self._logger.get_history()

	def log_cleanup(self) -> None:
		return self._logger.redis_cleanup()

	def get_device_count(self) -> int:
		return len(self._engine.devices)


class RolloutOrchestrator:
	def __init__(self, backend_obj: BackendServices, max_concurrent: int = 4)\
			->	None:
		self.max_concurrent = max_concurrent
		self._backend = backend_obj
		self._slots = threading.Semaphore(max_concurrent)
		self._jobs: dict[uuid.UUID, RolloutJob] = {}
		self._lock = threading.Lock()
		threading.Thread(target=self._dispatcher, daemon=True).start()

	def submit(self, devices: list[Device], commands: list[str], params:
	RolloutOptions, user_id: uuid.UUID,
	           comment: str | None = None) -> uuid.UUID:
		engine = RolloutEngine(params, devices, commands)
		job = RolloutJob(uuid.uuid4(), user_id, engine, params,
		                 redis_client=self._backend.redis.client)

		with self._lock:
			self._jobs[job.job_id] = job

		self._backend.redis.client.hset(f"job:{job.job_id}:meta", mapping={
			"user_id": str(user_id),
			"status": "pending",
			"device_count": job.get_device_count(),
			"created_at": datetime.datetime.now().isoformat(),
		})
		self._backend.redis.client.sadd(f"user_jobs:{user_id}", str(job.job_id))
		self._backend.redis.client.incr("netrollout:pending_count")

		with self._backend.postgres.get_session() as db_session:
			db_session.add(JobMetadata(job_id=job.job_id,
			                           user_id=user_id,
			                           commands=commands,
			                           comment=comment))

		self._backend.redis.client.rpush("netrollout:job_queue", str(job.job_id))
		return job.job_id

	def cancel(self, job_id: uuid.UUID) -> None:
		with self._lock:
			job = self._jobs.get(job_id, None)
		if job:
			job.cancel()
			self._backend.redis.client.hset(f"job:{job.job_id}:meta", field="status",
			                  value="cancelling")

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
				result = self._backend.redis.client.blpop(
					"netrollout:job_queue", timeout=_BLPOP_TIMEOUT)
			except REDIS_UNAVAILABLE as e:
				print(f"[NetRollout] dispatcher: Redis unavailable ({e}); "
				      f"retrying in {backoff}s", flush=True)
				time.sleep(backoff)
				backoff = min(backoff * 2, _BACKOFF_MAX)
				continue
			backoff = _BACKOFF_START
			if result is None:
				continue
			_, job_id_bytes = result
			try:
				job_id = uuid.UUID(job_id_bytes.decode())
			except ValueError:
				print(f"[NetRollout] dispatcher: skipping malformed queue "
				      f"entry {job_id_bytes!r}", flush=True)
				continue

			self._slots.acquire()

			with self._lock:
				job = self._jobs.get(job_id)
				if job is None:
					self._slots.release()
					continue

			job.start(self._cleanup)
			# The job is already running; a Redis failure here only leaves
			# status/counters stale, so it must not take the loop down.
			try:
				client = self._backend.redis.client
				client.hset(f"job:{job.job_id}:meta", "status", "active")
				client.hset(f"job:{job.job_id}:meta", "started_at",
				            datetime.datetime.now().isoformat())
				client.decr("netrollout:pending_count")
				client.incr("netrollout:active_count")
			except REDIS_UNAVAILABLE as e:
				print(f"[NetRollout] dispatcher: job {job.job_id} started but "
				      f"status update failed ({e})", flush=True)

	def _cleanup(self, job_id: uuid.UUID) -> None:
		try:
			self._finalize(job_id)
		finally:
			self._slots.release()

	def _finalize(self, job_id: uuid.UUID) -> None:
		with self._lock:
			job = self._jobs.pop(job_id, None)
		if job:
			with self._backend.postgres.get_session() as db_session:
				for result in job.results:
					db_session.add(DeviceResult(user_id=job.user_id,
					                            job_id=job.job_id,
					                            started_at=job.started_at,
					                            completed_at=datetime.datetime.now(),
					                            device_ip=result["device_ip"],
					                            device_type=result[
						                            "device_type"],
					                            commands_sent=result[
						                            "commands_sent"],
					                            commands_verified=result[
						                            "commands_verified"],
					                            fetched_config=result[
						                            "fetched_config"],
					                            status=result["status"]
					                            ))
				self._backend.redis.client.delete(f"job:{job.job_id}:meta")
				self._backend.redis.client.srem(f"user_jobs:{job.user_id}", str(job_id))
				self._backend.redis.client.decr("netrollout:active_count")
			job.log_cleanup()
