"""RolloutOrchestrator resilience, against in-memory fakes (no Redis/Postgres).

The orchestrator has no stop(), so each test leaves a daemon dispatcher
thread behind. FakeRedis.blpop therefore genuinely blocks (queue.get with a
timeout) so leftover threads sit idle instead of busy-looping.
"""
import queue
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import redis

import src.orchestration as orchestration
from src.core import RolloutEngine, RolloutOptions
from src.orchestration import RolloutOrchestrator

QUEUE = "netrollout:job_queue"


class FakeRedis:
	def __init__(self, blpop_failures=0):
		# Queues are created under a lock: a defaultdict's lazy creation isn't
		# atomic, so the dispatcher thread and the test could each create
		# their own Queue for the same key — the dispatcher then waits on an
		# orphan until its BLPOP timeout (a flaky test, not an app bug)
		self._queues = {}
		self._queues_lock = threading.Lock()
		self.blpop_failures = blpop_failures
		self.fail_writes = False
		self.hashes = defaultdict(dict)

	def _queue(self, key) -> queue.Queue:
		with self._queues_lock:
			return self._queues.setdefault(key, queue.Queue())

	def blpop(self, key, timeout=0):
		if self.blpop_failures > 0:
			self.blpop_failures -= 1
			raise redis.exceptions.ConnectionError("simulated drop")
		try:
			return key.encode(), self._queue(key).get(timeout=timeout or None)
		except queue.Empty:
			return None

	def rpush(self, key, value):
		self._queue(key).put(value.encode() if isinstance(value, str) else value)

	# Same signature as redis-py's hset — a permissive **kwargs here once hid
	# a real bug (orchestrator passing a nonexistent `field=` argument)
	def hset(self, name, key=None, value=None, mapping=None, items=None):
		if self.fail_writes:
			raise redis.exceptions.TimeoutError("simulated timeout")
		self.hashes[name].update(mapping or {key: value})

	# Counters / sets / pub-sub — state isn't asserted on, only must not fail
	def incr(self, *_): pass
	def decr(self, *_): pass
	def sadd(self, *_): pass
	def srem(self, *_): pass
	def delete(self, *_): pass
	def lrem(self, *_): pass
	def publish(self, *_): pass


class FakePostgres:
	def __init__(self):
		self.added = []

	@contextmanager
	def get_session(self):
		session = MagicMock()
		session.add.side_effect = self.added.append
		yield session


class FakeJob:
	"""Stands in for RolloutJob: records that the dispatcher started it."""

	def __init__(self):
		self.job_id = uuid.uuid4()
		self.user_id = uuid.uuid4()
		self.results = []
		self.started_at = None
		self.ran = threading.Event()

	def start(self, on_complete):
		self.started_at = time.time()
		self.ran.set()
		on_complete(self.job_id)

	def log_cleanup(self):
		pass


def wait_for(condition, timeout=5.0):
	deadline = time.time() + timeout
	while time.time() < deadline:
		if condition():
			return True
		time.sleep(0.02)
	return False


@pytest.fixture
def make_orchestrator(monkeypatch):
	# No real backoff sleeps: retries happen immediately
	monkeypatch.setattr(orchestration, "_BACKOFF_START", 0)

	def _make(fake_redis, max_concurrent=2):
		backend = SimpleNamespace(redis=SimpleNamespace(client=fake_redis),
		                          postgres=FakePostgres())
		return RolloutOrchestrator(backend, max_concurrent=max_concurrent)

	return _make


def enqueue(orch, fake_redis, job):
	with orch._lock:
		orch._jobs[job.job_id] = job
	fake_redis.rpush(QUEUE, str(job.job_id))


def dispatcher_thread(orch):
	return next(t for t in threading.enumerate()
	            if getattr(t, "_target", None) == orch._dispatcher)


# ── Dispatcher survives Redis failures ───────────────────────────────────────

def test_dispatcher_survives_connection_drops(make_orchestrator):
	fake = FakeRedis(blpop_failures=3)
	orch = make_orchestrator(fake)
	job = FakeJob()
	enqueue(orch, fake, job)
	assert job.ran.wait(timeout=5), "job never dispatched after Redis drops"
	assert dispatcher_thread(orch).is_alive()


def test_malformed_queue_entry_is_skipped(make_orchestrator):
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	fake.rpush(QUEUE, "not-a-uuid")
	job = FakeJob()
	enqueue(orch, fake, job)
	assert job.ran.wait(timeout=5)
	assert dispatcher_thread(orch).is_alive()


def test_status_write_failure_after_start_does_not_stop_dispatching(
		make_orchestrator):
	fake = FakeRedis()
	fake.fail_writes = True
	orch = make_orchestrator(fake)
	first, second = FakeJob(), FakeJob()
	enqueue(orch, fake, first)
	assert first.ran.wait(timeout=5)
	enqueue(orch, fake, second)
	assert second.ran.wait(timeout=5), "dispatcher died on status write failure"


def test_redis_unavailable_covers_timeouts():
	# An unreachable host raises TimeoutError, which is not a ConnectionError
	assert not issubclass(redis.exceptions.TimeoutError,
	                      redis.exceptions.ConnectionError)
	assert redis.exceptions.TimeoutError in orchestration.REDIS_UNAVAILABLE


# ── Engine crash releases the concurrency slot ───────────────────────────────

def test_engine_crash_releases_slot_and_next_job_runs(make_orchestrator):
	fake = FakeRedis()
	orch = make_orchestrator(fake, max_concurrent=1)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	# e.g. a mapping whose property is missing from the device's var_maps
	with patch.object(RolloutEngine, "run", side_effect=KeyError("hostname")):
		uid = uuid.uuid4()
		orch.submit([], ["cmd"], options, uid)
		orch.submit([], ["cmd"], options, uid)  # needs the single slot back
		assert wait_for(lambda: not orch._jobs), \
			"crashed job never cleaned up — slot leaked"
	assert orch._slots._value == 1


def test_cancel_marks_job_cancelling(make_orchestrator):
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	job = FakeJob()
	job.cancel = lambda: None
	with orch._lock:
		orch._jobs[job.job_id] = job
	orch.cancel(job.job_id)
	assert fake.hashes[f"job:{job.job_id}:meta"]["status"] == "cancelling"


def test_results_are_persisted_with_port(make_orchestrator):
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	result = {"device_ip": "10.9.9.9", "device_port": 2002,
	          "device_type": "cisco_ios", "commands_sent": 1,
	          "commands_verified": None, "fetched_config": None,
	          "status": "success"}
	with patch.object(RolloutEngine, "run", return_value=[result]):
		orch.submit([], ["cmd"], options, uuid.uuid4())
		assert wait_for(lambda: not orch._jobs)
	rows = [o for o in orch._backend.postgres.added
	        if type(o).__name__ == "DeviceResult"]
	assert [(r.device_ip, r.device_port) for r in rows] == [("10.9.9.9", 2002)]


def test_submit_records_job_metadata(make_orchestrator):
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	with patch.object(RolloutEngine, "run", return_value=[]):
		job_id = orch.submit([], ["hostname r1"], options, uuid.uuid4(),
		                     comment="change 42")
		assert wait_for(lambda: not orch._jobs)
	meta = [o for o in orch._backend.postgres.added
	        if type(o).__name__ == "JobMetadata"]
	assert len(meta) == 1
	assert meta[0].job_id == job_id and meta[0].comment == "change 42"


# ── Drain (stop / restart) ───────────────────────────────────────────────────

def _device(ip="10.0.0.1", port=22):
	return orchestration.Device(ip=ip, label=ip, username="u", password="p",
	                            device_type="cisco_ios", secret="", port=port)


def _rows(orch, status=None):
	return [o for o in orch._backend.postgres.added
	        if type(o).__name__ == "DeviceResult"
	        and (status is None or o.status == status)]


def _blocking_run(release: threading.Event, stop_on_cancel=False):
	"""An engine run that holds its slot until released (or cancelled)."""
	def run(cancel_flag, logger):
		while not release.is_set():
			if stop_on_cancel and cancel_flag.is_set():
				return [{"device_ip": "10.0.0.1", "device_port": 22,
				         "device_type": "cisco_ios", "commands_sent": 0,
				         "commands_verified": None, "fetched_config": None,
				         "status": "cancelled"}]
			time.sleep(0.02)
		return []
	return run


def test_submit_is_refused_while_draining(make_orchestrator):
	orch = make_orchestrator(FakeRedis())
	orch.drain(0)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	with pytest.raises(orchestration.Draining):
		orch.submit([_device()], ["cmd"], options, uuid.uuid4())
	assert orch.counts() == {"running": 0, "queued": 0}


def test_drain_records_queued_jobs_and_lets_running_ones_finish(
		make_orchestrator):
	orch = make_orchestrator(FakeRedis(), max_concurrent=1)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	release = threading.Event()
	with patch.object(RolloutEngine, "run", side_effect=_blocking_run(release)):
		uid = uuid.uuid4()
		orch.submit([_device()], ["cmd"], options, uid)
		assert wait_for(lambda: orch.counts()["running"] == 1)
		orch.submit([_device("10.0.0.2"), _device("10.0.0.3")], ["cmd"],
		            options, uid)            # waits for the single slot
		assert orch.counts() == {"running": 1, "queued": 1}

		done = threading.Event()
		threading.Thread(target=lambda: (orch.drain(30), done.set()),
		                 daemon=True).start()
		# The queued job is recorded as cancelled, device by device, at once
		assert wait_for(lambda: len(_rows(orch, "cancelled")) == 2)
		assert orch.draining and not done.is_set()   # the running one goes on
		release.set()
		assert done.wait(5), "drain didn't return once the running job ended"
	assert orch.counts() == {"running": 0, "queued": 0}
	assert {r.device_ip for r in _rows(orch, "cancelled")} == 	       {"10.0.0.2", "10.0.0.3"}


def test_drain_deadline_cancels_running_jobs(make_orchestrator):
	orch = make_orchestrator(FakeRedis())
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	never = threading.Event()
	with patch.object(RolloutEngine, "run",
	                  side_effect=_blocking_run(never, stop_on_cancel=True)):
		orch.submit([_device()], ["cmd"], options, uuid.uuid4())
		assert wait_for(lambda: orch.counts()["running"] == 1)
		lines = []
		orch.drain(0.2, report=lines.append)
	assert orch.counts()["running"] == 0
	assert [r.status for r in _rows(orch)] == ["cancelled"]
	assert any("Cancelling 1 running" in line for line in lines)


def test_dispatcher_does_not_start_a_job_once_draining(make_orchestrator):
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	orch._draining = True            # the drain began; a queue entry remains
	job = FakeJob()
	enqueue(orch, fake, job)
	time.sleep(0.3)
	assert not job.ran.is_set()
	assert orch._slots._value == 2   # the slot was given back
