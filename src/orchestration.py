import datetime
import threading
import uuid
from typing import Callable

from redis.client import PubSub

from core import RolloutEngine, RolloutOptions, Device, DeviceResultDict
from db import get_session
from db.redis_db import redis_client
from db.tables import DeviceResult, JobMetadata
from logging_utils import RolloutLogger


class RolloutJob:
	def __init__(self, job_id: uuid.UUID, user_id: uuid.UUID,
	             engine: RolloutEngine, options: RolloutOptions) -> None:
		self.job_id = job_id
		self.user_id = user_id
		self.started_at: datetime.datetime | None = None
		self.results: list[DeviceResultDict] = []
		self._engine = engine
		self._logger = RolloutLogger(options.webapp, options.verbose,
		                             job_id=str(job_id), prefix="rollout")
		self._cancel_flag = threading.Event()
		self._thread = None

	def start(self, on_complete: Callable[[uuid.UUID], None]) -> None:
		self.started_at = datetime.datetime.now()

		def _engine_run():
			self.results = self._engine.run(self._cancel_flag, self._logger)
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
	def __init__(self, max_concurrent: int = 4) -> None:
		self.max_concurrent = max_concurrent
		self._slots = threading.Semaphore(max_concurrent)
		self._jobs: dict[uuid.UUID, RolloutJob] = {}
		self._lock = threading.Lock()
		threading.Thread(target=self._dispatcher, daemon=True).start()

	def submit(self, devices: list[Device], commands: list[str], params:
	RolloutOptions, user_id: uuid.UUID,
	           comment: str | None = None) -> uuid.UUID:
		engine = RolloutEngine(params, devices, commands)
		job = RolloutJob(uuid.uuid4(), user_id, engine, params)

		with self._lock:
			self._jobs[job.job_id] = job

		redis_client.hset(f"job:{job.job_id}:meta", mapping={
			"user_id": str(user_id),
			"status": "pending",
			"device_count": job.get_device_count(),
			"created_at": datetime.datetime.now().isoformat(),
		})
		redis_client.sadd(f"user_jobs:{user_id}", str(job.job_id))
		redis_client.incr("netrollout:pending_count")

		with get_session() as db_session:
			db_session.add(JobMetadata(job_id=job.job_id,
			                           user_id=user_id,
			                           commands=commands,
			                           comment=comment))

		redis_client.rpush("netrollout:job_queue", str(job.job_id))
		return job.job_id

	def cancel(self, job_id: uuid.UUID) -> None:
		with self._lock:
			job = self._jobs.get(job_id, None)
		if job:
			job.cancel()
			redis_client.hset(f"job:{job.job_id}:meta", field="status",
			                  value="cancelling")

	def get_job(self, job_id: uuid.UUID) -> RolloutJob | None:
		with self._lock:
			job = self._jobs.get(job_id, None)
		return job

	def _dispatcher(self) -> None:
		while True:
			result = redis_client.blpop("netrollout:job_queue", timeout=0)
			if result is None:
				continue
			_, job_id_bytes = result
			job_id = uuid.UUID(job_id_bytes.decode())

			self._slots.acquire()

			with self._lock:
				job = self._jobs.get(job_id)
				if job is None:
					self._slots.release()
					continue

			job.start(self._cleanup)
			redis_client.hset(f"job:{job.job_id}:meta", "status", "active")
			redis_client.hset(f"job:{job.job_id}:meta", "started_at",
			                  datetime.datetime.now().isoformat())
			redis_client.decr("netrollout:pending_count")
			redis_client.incr("netrollout:active_count")

	def _cleanup(self, job_id: uuid.UUID) -> None:
		with self._lock:
			job = self._jobs.pop(job_id, None)
		if job:
			with get_session() as db_session:
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
				redis_client.delete(f"job:{job.job_id}:meta")
				redis_client.srem(f"user_jobs:{job.user_id}", str(job_id))
				redis_client.decr("netrollout:active_count")
			job.log_cleanup()
		self._slots.release()
