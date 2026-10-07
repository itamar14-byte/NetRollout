"""RolloutOrchestrator resilience, against in-memory fakes (no Redis/Postgres).

The orchestrator has no stop(), so each test leaves a daemon dispatcher
thread behind. FakeRedis.blpop therefore genuinely blocks (queue.get with a
timeout) so leftover threads sit idle instead of busy-looping.
"""
import json
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

import src.jobs as orchestration
from src.jobs import RolloutOrchestrator
from src.rollout.engine import RolloutEngine, RolloutOptions


QUEUE = "netrollout:job_queue"


class FakeRedis:
	"""In-memory Redis for the orchestrator: real blocking queues, hashes, a
	number of BLPOP connection drops and failing hash writes on demand."""
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
	"""Postgres whose sessions record every added row in `added`."""
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
	"""Poll `condition` until true or `timeout` seconds pass; returns whether it held."""
	deadline = time.time() + timeout
	while time.time() < deadline:
		if condition():
			return True
		time.sleep(0.02)
	return False


@pytest.fixture
def make_orchestrator(monkeypatch):
	"""Build a RolloutOrchestrator on a given FakeRedis and a FakePostgres,
	with no backoff sleeps."""
	# No real backoff sleeps: retries happen immediately
	monkeypatch.setattr(orchestration, "_BACKOFF_START", 0)

	def _make(fake_redis, max_concurrent=2):
		backend = SimpleNamespace(redis=SimpleNamespace(client=fake_redis),
		                          postgres=FakePostgres())
		return RolloutOrchestrator(backend, max_concurrent=max_concurrent)

	return _make


def enqueue(orch, fake_redis, job):
	"""Register a job with the orchestrator and push its id onto the queue."""
	with orch._lock:
		orch._jobs[job.job_id] = job
	fake_redis.rpush(QUEUE, str(job.job_id))


def dispatcher_thread(orch):
	"""The orchestrator's running dispatcher thread."""
	return next(t for t in threading.enumerate()
	            if getattr(t, "_target", None) == orch._dispatcher)


# ── Dispatcher survives Redis failures ───────────────────────────────────────

def test_dispatcher_survives_connection_drops(make_orchestrator):
	"""Three Redis connection drops in BLPOP don't stop the dispatcher: the
	queued job still runs and the thread stays alive."""
	fake = FakeRedis(blpop_failures=3)
	orch = make_orchestrator(fake)
	job = FakeJob()
	enqueue(orch, fake, job)
	assert job.ran.wait(timeout=5), "job never dispatched after Redis drops"
	assert dispatcher_thread(orch).is_alive()


class ClosedUnderneathRedis(FakeRedis):
	"""A Redis switch closes the client the dispatcher's wait is blocked on -
	redis-py then raises ValueError, not a connection error (seen on Windows)."""
	def __init__(self):
		super().__init__()
		self.closed_once = False

	def blpop(self, key, timeout=0):
		if not self.closed_once:
			self.closed_once = True
			raise ValueError("I/O operation on closed file.")
		return super().blpop(key, timeout)


def test_dispatcher_survives_its_client_closed_underneath(make_orchestrator):
	"""A ValueError from BLPOP (the client closed by a Redis switch) doesn't
	kill the dispatcher: the job runs and the thread stays alive."""
	fake = ClosedUnderneathRedis()
	orch = make_orchestrator(fake)
	job = FakeJob()
	enqueue(orch, fake, job)
	assert job.ran.wait(timeout=5), "the dispatcher died with its closed client"
	assert dispatcher_thread(orch).is_alive()


def test_malformed_queue_entry_is_skipped(make_orchestrator):
	"""A queue entry that isn't a UUID is skipped: the next job runs and the
	dispatcher stays alive."""
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	fake.rpush(QUEUE, "not-a-uuid")
	job = FakeJob()
	enqueue(orch, fake, job)
	assert job.ran.wait(timeout=5)
	assert dispatcher_thread(orch).is_alive()


def test_status_write_failure_after_start_does_not_stop_dispatching(
		make_orchestrator):
	"""With every Redis status write failing, the dispatcher still runs the first
	job and then the second."""
	fake = FakeRedis()
	fake.fail_writes = True
	orch = make_orchestrator(fake)
	first, second = FakeJob(), FakeJob()
	enqueue(orch, fake, first)
	assert first.ran.wait(timeout=5)
	enqueue(orch, fake, second)
	assert second.ran.wait(timeout=5), "dispatcher died on status write failure"


def test_redis_unavailable_covers_timeouts():
	"""An unreachable host raises TimeoutError, which is not a ConnectionError:
	REDIS_UNAVAILABLE must include it."""
	assert not issubclass(redis.exceptions.TimeoutError,
	                      redis.exceptions.ConnectionError)
	assert redis.exceptions.TimeoutError in orchestration.REDIS_UNAVAILABLE


# ── Engine crash releases the concurrency slot ───────────────────────────────

def test_engine_crash_releases_slot_and_next_job_runs(make_orchestrator):
	"""An engine crash (KeyError) in a job gives its single slot back: the next
	job runs, both are cleaned up and the slot count is back to 1."""
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
	"""cancel() sets the job's Redis status to "cancelling"."""
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	job = FakeJob()
	job.cancel = lambda: None
	with orch._lock:
		orch._jobs[job.job_id] = job
	orch.cancel(job.job_id)
	assert fake.hashes[f"job:{job.job_id}:meta"]["status"] == "cancelling"


def test_results_are_persisted_with_port(make_orchestrator):
	"""A job's results are stored as DeviceResult rows with the device's port."""
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


def test_the_action_needed_instruction_is_stored(make_orchestrator):
	"""A result's action_needed instruction is stored on its DeviceResult row."""
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	result = {"device_ip": "10.9.9.9", "device_port": 22,
	          "device_type": "checkpoint_gaia", "commands_sent": 1,
	          "commands_verified": None, "fetched_config": None,
	          "status": "success", "action_needed": "save it on the device"}
	with patch.object(RolloutEngine, "run", return_value=[result]):
		orch.submit([], ["cmd"], options, uuid.uuid4())
		assert wait_for(lambda: not orch._jobs)
	(row,) = [o for o in orch._backend.postgres.added
	          if type(o).__name__ == "DeviceResult"]
	assert row.action_needed == "save it on the device"


def test_submit_records_job_metadata(make_orchestrator):
	"""submit() stores one JobMetadata row with the job id and the comment."""
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
	"""A cisco_ios Device at ip:port with dummy credentials."""
	return orchestration.Device(ip=ip, label=ip, username="u", password="p",
	                            device_type="cisco_ios", secret="", port=port)


def _rows(orch, status=None):
	"""The DeviceResult rows the orchestrator stored, optionally of one status."""
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
	"""Once draining, submit() raises Draining and nothing is running or queued."""
	orch = make_orchestrator(FakeRedis())
	orch.drain(0)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	with pytest.raises(orchestration.Draining):
		orch.submit([_device()], ["cmd"], options, uuid.uuid4())
	assert orch.counts() == {"running": 0, "queued": 0}


def test_drain_records_queued_jobs_and_lets_running_ones_finish(
		make_orchestrator):
	"""drain() records the queued job as cancelled at once, device by device,
	and waits for the running job to finish before returning; nothing is left
	running or queued."""
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
	assert {r.device_ip for r in _rows(orch, "cancelled")} == \
	       {"10.0.0.2", "10.0.0.3"}


def test_drain_deadline_cancels_running_jobs(make_orchestrator):
	"""A running job still going at the drain's deadline is cancelled: recorded
	as cancelled, nothing running, and the report says "Cancelling 1 running"."""
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
	"""While draining, a job left in the queue isn't started and its slot is
	given back."""
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	orch._draining = True            # the drain began; a queue entry remains
	job = FakeJob()
	enqueue(orch, fake, job)
	time.sleep(0.3)
	assert not job.ran.is_set()
	assert orch._slots._value == 2   # the slot was given back


# ── The end of a job: results never lost, state never stuck ─────────────────

RESULT = dict(device_ip="10.0.0.1", device_port=22, device_type="cisco_ios",
              commands_sent=1, commands_verified=None, fetched_config=None,
              status="success", action_needed=None)


class TrackingRedis(FakeRedis):
	"""Records the end-of-job cleanup; `fail_cleanup` makes it raise."""

	def __init__(self):
		super().__init__()
		self.deleted, self.fail_cleanup = [], False

	def delete(self, *keys):
		if self.fail_cleanup:
			raise redis.exceptions.ConnectionError("simulated drop")
		self.deleted.extend(keys)


class FlakyPostgres(FakePostgres):
	"""Fails the first `failures` sessions (None: always)."""

	def __init__(self, failures=None):
		super().__init__()
		self.failures = failures

	@contextmanager
	def get_session(self):
		if self.failures is None or self.failures > 0:
			if self.failures:
				self.failures -= 1
			raise RuntimeError("simulated: database unreachable")
		with super().get_session() as session:
			yield session


class EndedJob(FakeJob):
	"""A FakeJob with one result whose log_cleanup marks the job's very end."""
	def __init__(self):
		super().__init__()
		self.results = [dict(RESULT)]
		self.cleaned = threading.Event()

	def log_cleanup(self):
		self.cleaned.set()


@pytest.fixture
def end_of_job(monkeypatch, tmp_path):
	"""Run one EndedJob through an orchestrator on the given Redis and Postgres
	(no retry waits, NETROLLOUT_HOME in tmp_path) until it is finalized and its
	slot free; returns (orchestrator, job)."""
	monkeypatch.setattr(orchestration, "_BACKOFF_START", 0)
	monkeypatch.setattr(orchestration, "_SAVE_RETRY_WAIT", 0, raising=False)
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))

	def run(fake_redis, postgres):
		orch = RolloutOrchestrator(SimpleNamespace(
			redis=SimpleNamespace(client=fake_redis), postgres=postgres),
			max_concurrent=1)
		job = EndedJob()
		enqueue(orch, fake_redis, job)
		# popped from the job table first, then saved: wait for the very end
		assert job.cleaned.wait(timeout=10), "job never finalized"
		assert wait_for(lambda: orch._slots._value == 1)
		return orch, job
	return run


def test_results_go_to_a_file_when_postgres_stays_down(end_of_job, tmp_path,
                                                       capsys):
	"""Postgres down for good: the results go to logs/unsaved-results-<job>.json
	with an ACTION NEEDED line, and the job's Redis meta is still deleted, its
	log closed and its slot free."""
	fake = TrackingRedis()
	orch, job = end_of_job(fake, FlakyPostgres())
	saved = tmp_path / "logs" / f"unsaved-results-{job.job_id}.json"
	data = json.loads(saved.read_text(encoding="utf-8"))
	assert data["job_id"] == str(job.job_id) and data["results"] == [RESULT]
	assert "ACTION NEEDED" in capsys.readouterr().out
	# and the job doesn't stay "active", its log is closed, its slot free
	assert f"job:{job.job_id}:meta" in fake.deleted
	assert job.cleaned.is_set() and orch._slots._value == 1


def test_a_brief_postgres_outage_is_retried(end_of_job, tmp_path):
	"""A single failed Postgres session is retried: the result is stored and no
	unsaved-results file is written."""
	postgres = FlakyPostgres(failures=1)
	_, job = end_of_job(TrackingRedis(), postgres)
	assert [r.status for r in postgres.added] == ["success"]
	assert not (tmp_path / "logs").exists()


def test_results_are_saved_when_redis_is_down_at_the_end(end_of_job):
	"""A Redis failure in the end-of-job cleanup doesn't roll back the results:
	they are stored and the slot is free."""
	fake = TrackingRedis()
	fake.fail_cleanup = True
	postgres = FakePostgres()
	orch, job = end_of_job(fake, postgres)
	# the Redis failure no longer rolls the database write back
	assert [(r.device_ip, r.job_id) for r in postgres.added] == \
	       [("10.0.0.1", job.job_id)]
	assert orch._slots._value == 1


# ── Paused for a database move (src/webapp/db_move.py) ───────────────────

def test_pause_refuses_new_rollouts_lets_queued_and_running_ones_finish(
		make_orchestrator):
	"""pause() refuses new rollouts with PAUSED_MESSAGE but cancels nothing: the
	running and the queued job both finish, then it is idle; resume() clears
	the refusal."""
	orch = make_orchestrator(FakeRedis(), max_concurrent=1)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	release = threading.Event()
	with patch.object(RolloutEngine, "run", side_effect=_blocking_run(release)):
		uid = uuid.uuid4()
		orch.submit([_device()], ["cmd"], options, uid)
		assert wait_for(lambda: orch.counts()["running"] == 1)
		orch.submit([_device("10.0.0.2")], ["cmd"], options, uid)   # queued
		orch.pause()
		with pytest.raises(orchestration.Paused) as refused:
			orch.submit([_device("10.0.0.3")], ["cmd"], options, uid)
		assert str(refused.value) == orchestration.PAUSED_MESSAGE
		assert orch.refusal() == orchestration.PAUSED_MESSAGE
		assert not orch.idle()
		release.set()
		# nothing cancelled: the queued one runs too, then the pause is idle
		assert wait_for(orch.idle)
	assert _rows(orch, "cancelled") == []
	orch.resume()
	assert orch.refusal() is None and not orch.paused


def test_resume_never_undoes_a_drain(make_orchestrator):
	"""resume() after a drain leaves submit() refused with Draining (not Paused)
	and the drain's message."""
	orch = make_orchestrator(FakeRedis())
	orch.pause()
	orch.drain(0)
	orch.resume()
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	with pytest.raises(orchestration.Draining) as refused:
		orch.submit([_device()], ["cmd"], options, uuid.uuid4())
	assert type(refused.value) is orchestration.Draining     # not Paused
	assert orch.refusal() == orchestration.DRAINING_MESSAGE


class SlowPostgres(FakePostgres):
	"""Writing the results waits until `go` is set."""

	def __init__(self):
		super().__init__()
		self.go = threading.Event()
		self.writing = threading.Event()

	@contextmanager
	def get_session(self):
		self.writing.set()
		self.go.wait(5)
		with super().get_session() as session:
			yield session


def test_not_idle_while_a_finished_rollout_still_writes_its_results(
		monkeypatch, tmp_path):
	"""The job leaves the job table before its results are written: a move
	locking then would copy the data without them. So idle() stays false until
	the results are written."""
	monkeypatch.setattr(orchestration, "_BACKOFF_START", 0)
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	postgres, fake = SlowPostgres(), FakeRedis()
	orch = RolloutOrchestrator(SimpleNamespace(
		redis=SimpleNamespace(client=fake), postgres=postgres), max_concurrent=1)
	job = EndedJob()
	enqueue(orch, fake, job)
	assert postgres.writing.wait(5)
	assert orch.counts() == {"running": 0, "queued": 0}   # gone from the table
	assert not orch.idle()                                 # but still writing
	postgres.go.set()
	assert job.cleaned.wait(5)
	assert wait_for(orch.idle)
