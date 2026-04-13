import datetime
import threading
import uuid
from typing import Callable


from core import RolloutEngine, RolloutOptions, Device, DeviceResultDict
from db import get_session
from logging_utils import RolloutLogger
from tables import RolloutSession, DeviceResult, JobMetadata


class RolloutJob:
	def __init__(self, job_id: uuid.UUID, user_id: uuid.UUID,
	             engine: RolloutEngine, options: RolloutOptions) -> None:
		self.job_id = job_id
		self.user_id = user_id
		self.started_at: datetime.datetime | None = None
		self.results: list[DeviceResultDict] = []
		self._engine = engine
		ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
		self._logger = RolloutLogger(options.webapp, options.verbose,
		                             job_id=str(job_id), timestamp=ts)
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

	def is_pending(self) -> bool:
		return self._thread is None

	def get_log_queue(self) -> str:
		return self._logger.get_queue(1)

	def get_log_history(self):
		return self._logger.get_buffer_snapshot()

	def get_device_count(self) -> int:
		return len(self._engine.devices)


class RolloutOrchestrator:
	def __init__(self, max_concurrent: int = 4) -> None:
		self.max_concurrent = max_concurrent
		self._jobs: dict[uuid.UUID, RolloutJob] = {}
		self._lock = threading.Lock()

	def submit(self, devices: list[Device], commands: list[str], params:
	RolloutOptions, user_id: uuid.UUID, comment: str | None = None) -> uuid.UUID:
		engine = RolloutEngine(params, devices, commands)
		job = RolloutJob(uuid.uuid4(), user_id, engine, params)

		with self._lock:
			self._jobs[job.job_id] = job
		with get_session() as db_session:
			db_session.add(RolloutSession(id=job.job_id,
			                              user_id=user_id,
			                              status="pending"
			                              ))
			db_session.add(JobMetadata(job_id = job.job_id,
			                           user_id = user_id,
			                           commands=commands,
			                           comment=comment))

		self._dispatch()

		return job.job_id

	def cancel(self, job_id: uuid.UUID) -> None:
		with self._lock:
			job = self._jobs.get(job_id, None)
		if job:
			job.cancel()
			with get_session() as db_session:
				session_row = db_session.get(RolloutSession, job.job_id)
				if session_row:
					session_row.status = "cancelling"

	def get(self, job_id: uuid.UUID) -> RolloutJob | None:
		with self._lock:
			job = self._jobs.get(job_id, None)
		return job

	def _dispatch(self) -> None:
		with self._lock:
			num_active = sum(1 for job in self._jobs.values() if job.is_alive())
			pending = [job for job in self._jobs.values() if
			           job.is_pending()]
		for job in pending:
			if num_active >= self.max_concurrent:
				break
			job.start(self._cleanup)
			num_active += 1
			with get_session() as db_session:
				session_row = db_session.get(RolloutSession, job.job_id)
				if session_row:
					session_row.status = "active"

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
					                            device_type=result["device_type"],
					                            commands_sent=result["commands_sent"],
					                            commands_verified=result[
						                            "commands_verified"],
					                            status=result["status"]
					                            ))
				session_row = db_session.get(RolloutSession, job.job_id)
				if session_row:
					db_session.delete(session_row)

		self._dispatch()
