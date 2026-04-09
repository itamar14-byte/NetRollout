import threading
import uuid
from typing import Callable

from core import RolloutEngine, RolloutOptions, Device
from logging_utils import RolloutLogger


class RolloutJob:
	def __init__(self, job_id: uuid.UUID, engine: RolloutEngine,
	             options: RolloutOptions) -> None:
		self.id = job_id
		self._engine = engine
		self._logger = RolloutLogger(options.webapp, options.verbose)
		self._cancel_flag = threading.Event()
		self._thread = None

	def start(self, on_complete: Callable[[uuid.UUID], None]) -> None:
		def _engine_run():
			self._engine.run(self._cancel_flag, self._logger)
			on_complete(self.id)

		self._thread = threading.Thread(target=_engine_run, daemon=True)
		self._thread.start()

	def cancel(self) -> None:
		self._cancel_flag.set()

	def is_alive(self) -> bool:
		return self._thread is not None and self._thread.is_alive()

	def is_pending(self) -> bool:
		return self._thread is None

	def get_log(self) -> str:
		return self._logger.get()


class RolloutOrchestrator:
	def __init__(self, max_concurrent: int = 4) -> None:
		self.max_concurrent = max_concurrent
		self._jobs: dict[uuid.UUID, RolloutJob] = {}
		self._lock = threading.Lock()

	def submit(self, devices: list[Device], commands: list[str],
	           params: RolloutOptions) -> uuid.UUID:
		engine = RolloutEngine(params, devices, commands)
		job = RolloutJob(uuid.uuid4(), engine, params)

		with self._lock:
			self._jobs[job.id] = job
		# TODO add write to DB rollutsession
		self._dispatch()

		return job.id

	def cancel(self, job_id: uuid.UUID) -> None:
		with self._lock:
			job = self._jobs.get(job_id,None)
		if job:
			job.cancel()
			#TODO update DB rollout session

	def get(self, job_id: uuid.UUID) -> RolloutJob | None:
		with self._lock:
			job = self._jobs.get(job_id,None)
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

	def _cleanup(self, job_id: uuid.UUID) -> None:
		with self._lock:
			self._jobs.pop(job_id, None)
		# TODO: write DeviceResult rows, delete RolloutSession
		self._dispatch()
