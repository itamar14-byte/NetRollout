"""RolloutOrchestrator resilience, against in-memory fakes (no Redis/Postgres).

The orchestrator has no stop(), so each test leaves a daemon dispatcher
thread behind. FakeRedis.blpop therefore genuinely blocks (queue.get with a
timeout) so leftover threads sit idle instead of busy-looping.
"""
import datetime as dt
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

from src import jobs
from src.db.settings import SETTINGS
from src.jobs import (JOB_STATUSES, JobStore, RolloutOrchestrator, job_status,
                      job_status_condition, build_kpi)
from src.rollout.engine import RolloutEngine, RolloutOptions
from src.webapp.blueprints.jobs import config_expired


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
		self.values = {}

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
		q = self._queue(key)
		q.put(value.encode() if isinstance(value, str) else value)
		return q.qsize()           # redis-py: the list's length after the push

	# Same signature as redis-py's hset — a permissive **kwargs here once hid
	# a real bug (orchestrator passing a nonexistent `field=` argument)
	def hset(self, name, key=None, value=None, mapping=None, items=None):
		if self.fail_writes:
			raise redis.exceptions.TimeoutError("simulated timeout")
		self.hashes[name].update(mapping or {key: value})

	def eval(self, script, numkeys, key, field, value):
		"""JobStore's one script (_SET_IF_EXISTS): HSET only on an existing hash
		(the real script runs in tests/integration/test_job_store.py)."""
		assert script == jobs._SET_IF_EXISTS and numkeys == 1
		if self.fail_writes:
			raise redis.exceptions.TimeoutError("simulated timeout")
		if self.hashes.get(key):
			self.hashes[key][field] = value
			return 1
		return 0

	# Counters and sets
	def incr(self, key):
		self.values[key] = int(self.values.get(key, 0)) + 1

	def decr(self, key):
		self.values[key] = int(self.values.get(key, 0)) - 1

	def sadd(self, key, member):
		self.values.setdefault(key, set()).add(member)

	def srem(self, key, member):
		self.values.get(key, set()).discard(member)

	def smembers(self, key):
		return set(self.values.get(key, set()))

	def get(self, key):
		return self.values.get(key)

	def delete(self, *keys):
		for key in keys:
			self.values.pop(key, None)
			self.hashes.pop(key, None)

	def set(self, name, value, ex=None):
		self.values[name] = value

	def exists(self, *names):
		return sum(name in self.values for name in names)

	def scan_iter(self, pattern):
		return iter(())

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
	monkeypatch.setattr(jobs, "_BACKOFF_START", 0)

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
	assert redis.exceptions.TimeoutError in jobs.REDIS_UNAVAILABLE


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
	"""cancel() of a running job sets its Redis status to "cancelling"."""
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	job = FakeJob()
	job.cancel = lambda: None
	job.started_at = time.time()                  # running, its hash there
	fake.hashes[f"job:{job.job_id}:meta"] = {"status": "active"}
	with orch._lock:
		orch._jobs[job.job_id] = job
	orch.cancel(job.job_id)
	assert fake.hashes[f"job:{job.job_id}:meta"]["status"] == "cancelling"


def test_a_cancel_racing_the_jobs_end_leaves_no_phantom_row(make_orchestrator):
	"""The job ends (its hash deleted) between cancel() finding it and writing
	"cancelling": the status isn't written - no hash comes back to show a job
	that never ends on Active Jobs."""
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	store = JobStore(SimpleNamespace(client=fake))
	job = FakeJob()
	job.started_at = time.time()
	store.add(job.job_id, job.user_id, 1)
	store.started(job.job_id)
	with orch._lock:
		orch._jobs[job.job_id] = job
	# the job's end lands right inside the cancel
	job.cancel = lambda: store.finished(job.job_id, job.user_id, was_running=True)
	orch.cancel(job.job_id)
	assert f"job:{job.job_id}:meta" not in fake.hashes
	assert store.job_ids(job.user_id) == [] and store.counts() == (0, 0)


def test_a_job_that_ends_at_once_leaves_no_phantom_row(make_orchestrator):
	"""A job that ends the moment it starts (FakeJob calls on_complete inside
	start) is finalized before the dispatcher would mark it active: its hash
	must not come back as "active" afterwards."""
	fake = FakeRedis()
	orch = make_orchestrator(fake, max_concurrent=1)
	job = FakeJob()
	JobStore(SimpleNamespace(client=fake)).add(job.job_id, job.user_id, 1)
	enqueue(orch, fake, job)
	assert job.ran.wait(5)
	assert wait_for(lambda: orch._slots._value == 1)
	time.sleep(0.1)
	assert f"job:{job.job_id}:meta" not in fake.hashes
	assert fake.values.get(jobs.ACTIVE, 0) == 0 and fake.values.get(jobs.PENDING, 0) == 0


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
	return jobs.Device(ip=ip, label=ip, username="u", password="p",
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
	with pytest.raises(jobs.Draining):
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
	monkeypatch.setattr(jobs, "_BACKOFF_START", 0)
	monkeypatch.setattr(jobs, "_SAVE_RETRY_WAIT", 0, raising=False)
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
		with pytest.raises(jobs.Paused) as refused:
			orch.submit([_device("10.0.0.3")], ["cmd"], options, uid)
		assert str(refused.value) == jobs.PAUSED_MESSAGE
		assert orch.refusal() == jobs.PAUSED_MESSAGE
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
	with pytest.raises(jobs.Draining) as refused:
		orch.submit([_device()], ["cmd"], options, uuid.uuid4())
	assert type(refused.value) is jobs.Draining     # not Paused
	assert orch.refusal() == jobs.DRAINING_MESSAGE


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
	monkeypatch.setattr(jobs, "_BACKOFF_START", 0)
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


def test_a_stop_waits_for_the_results_being_written(monkeypatch, tmp_path):
	"""A stop / restart drains: a job that just ended has left the job table but
	is still writing its results - the drain waits for them (else the exit cut
	the save and the results were lost), then returns."""
	monkeypatch.setattr(jobs, "_BACKOFF_START", 0)
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	postgres, fake = SlowPostgres(), FakeRedis()
	orch = RolloutOrchestrator(SimpleNamespace(
		redis=SimpleNamespace(client=fake), postgres=postgres), max_concurrent=1)
	job = EndedJob()
	enqueue(orch, fake, job)
	assert postgres.writing.wait(5)
	assert orch.counts() == {"running": 0, "queued": 0}
	drained = threading.Event()
	threading.Thread(target=lambda: (orch.drain(30, report=lambda _: None), drained.set()),
	                 daemon=True).start()
	assert not drained.wait(0.5)            # still writing: the drain waits
	postgres.go.set()
	assert drained.wait(5)
	assert postgres.added                   # the results were written


def test_a_stop_now_request_cancels_the_running_rollouts_at_once(make_orchestrator):
	"""`netrollout stop` / the Manager can ask for a stop now (JobStore's stop-now
	mark): a drain then doesn't wait out its deadline - running rollouts are
	cancelled at once (and recorded); a new start clears the mark."""
	fake = FakeRedis()
	orch = make_orchestrator(fake)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	never = threading.Event()
	with patch.object(RolloutEngine, "run",
	                  side_effect=_blocking_run(never, stop_on_cancel=True)):
		orch.submit([_device()], ["cmd"], options, uuid.uuid4())
		assert wait_for(lambda: orch.counts()["running"] == 1)
		store = JobStore(SimpleNamespace(client=fake))
		assert not store.stop_now_requested()
		store.request_stop_now()
		assert store.stop_now_requested()
		started = time.monotonic()
		lines = []
		orch.drain(600, report=lines.append)
	assert time.monotonic() - started < 30
	assert orch.counts()["running"] == 0
	assert any("Cancelling 1 running" in line for line in lines)
	store.reset_stale()
	assert not store.stop_now_requested()


# ── The live log is best-effort: a Redis outage loses no result ─────────────

class LiveLogDownRedis(FakeRedis):
	"""The job queue works; the live log (history list, pub/sub) raises."""

	def rpush(self, key, value):
		if key.endswith(":history"):
			raise redis.exceptions.ConnectionError("simulated outage")
		return super().rpush(key, value)

	def publish(self, *_):
		raise redis.exceptions.ConnectionError("simulated outage")


def _log_text(tmp_path):
	"""Every rollout log file's text under tmp_path/logs."""
	return "".join(p.read_text(encoding="utf-8")
	               for p in (tmp_path / "logs").glob("rollout_*.log"))


def test_a_redis_outage_mid_rollout_keeps_every_result_and_log_line(
		make_orchestrator, monkeypatch, tmp_path):
	"""Redis fails every live-log write while a web rollout runs: every device
	result is still recorded and the log file holds every line."""
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	orch = make_orchestrator(LiveLogDownRedis())
	options = RolloutOptions(verify=False, verbose=False, webapp=True)

	def run(cancel_flag, logger):
		logger.notify("10.0.0.1: configured", important=True)
		logger.notify("10.0.0.2: refused the login", "red")
		return [dict(RESULT), dict(RESULT, device_ip="10.0.0.2", status="failed")]

	with patch.object(RolloutEngine, "run", side_effect=run):
		orch.submit([_device(), _device("10.0.0.2")], ["cmd"], options, uuid.uuid4())
		assert wait_for(orch.idle)
	assert sorted((r.device_ip, r.status) for r in _rows(orch)) == \
	       [("10.0.0.1", "success"), ("10.0.0.2", "failed")]
	text = _log_text(tmp_path)
	assert "10.0.0.1: configured" in text and "10.0.0.2: refused the login" in text


def test_a_redis_outage_during_a_drain_still_records_the_queued_rollout(
		make_orchestrator, monkeypatch, tmp_path):
	"""A queued web rollout cancelled by a drain while the live log is down is
	still recorded (every device cancelled) with the reason in its log file."""
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	orch = make_orchestrator(LiveLogDownRedis(), max_concurrent=1)
	options = RolloutOptions(verify=False, verbose=False, webapp=True)
	release = threading.Event()
	with patch.object(RolloutEngine, "run", side_effect=_blocking_run(release)):
		orch.submit([_device()], ["cmd"], options, uuid.uuid4())
		assert wait_for(lambda: orch.counts()["running"] == 1)
		orch.submit([_device("10.0.0.2")], ["cmd"], options, uuid.uuid4())
		threading.Timer(0.3, release.set).start()
		orch.drain(30, report=lambda _: None)
	assert [(r.device_ip, r.status) for r in _rows(orch, "cancelled")] == \
	       [("10.0.0.2", "cancelled")]
	assert jobs.QUEUED_CANCEL_REASON in _log_text(tmp_path)


# ── submit() failing partway leaves nothing behind ───────────────────────────

class EnqueueFailsRedis(FakeRedis):
	"""Queuing a job fails (everything else works)."""

	def rpush(self, key, value):
		if key == QUEUE:
			raise redis.exceptions.ConnectionError("simulated drop")
		return super().rpush(key, value)


@pytest.mark.parametrize("failing", ["store.add", "metadata", "enqueue"])
def test_a_submit_failing_partway_leaves_no_job(make_orchestrator, failing):
	"""submit() raises when a step fails - recording the job in Redis, its
	metadata in Postgres, queuing it - and leaves no job: idle, nothing
	counted (here or in Redis), no job keys."""
	fake = EnqueueFailsRedis() if failing == "enqueue" else FakeRedis()
	fake.fail_writes = failing == "store.add"
	orch = make_orchestrator(fake)
	if failing == "metadata":
		orch._backend.postgres = FlakyPostgres()
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	uid = uuid.uuid4()
	with pytest.raises(Exception):
		orch.submit([_device()], ["cmd"], options, uid)
	assert orch.idle() and orch.counts() == {"running": 0, "queued": 0}
	store = JobStore(SimpleNamespace(client=fake))
	assert store.counts() == (0, 0) and store.job_ids(uid) == []
	assert not [k for k in fake.hashes if k.startswith("job:")]


# ── Cancelling a queued job ends it at once ──────────────────────────────────

class QueuedJob(FakeJob):
	"""A FakeJob that can be recorded as cancelled before it starts."""

	def __init__(self):
		super().__init__()
		self.reasons = []
		self.cancel = lambda: None

	def cancel_before_start(self, reason):
		self.started_at = time.time()
		self.reasons.append(reason)
		self.results = [dict(RESULT, status="cancelled")]


def test_cancelling_a_queued_job_records_it_at_once(make_orchestrator):
	"""A queued rollout (its slot taken by a running one) cancelled: recorded as
	cancelled at once - it doesn't wait for a slot - and never runs, even
	once the slot frees."""
	orch = make_orchestrator(FakeRedis(), max_concurrent=1)
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	release, runs = threading.Event(), []
	blocking = _blocking_run(release)

	def run(cancel_flag, logger):
		runs.append(1)
		return blocking(cancel_flag, logger)

	with patch.object(RolloutEngine, "run", side_effect=run):
		uid = uuid.uuid4()
		orch.submit([_device()], ["cmd"], options, uid)
		assert wait_for(lambda: orch.counts()["running"] == 1)
		queued = orch.submit([_device("10.0.0.2")], ["cmd"], options, uid)
		orch.cancel(queued)
		# at once, while the running one still holds the slot
		assert [r.device_ip for r in _rows(orch, "cancelled")] == ["10.0.0.2"]
		assert orch.counts() == {"running": 1, "queued": 0}
		release.set()
		assert wait_for(orch.idle)
		time.sleep(0.3)
	assert len(runs) == 1


def test_a_queued_job_cancelled_while_the_dispatcher_waits_for_a_slot_never_starts(
		make_orchestrator):
	"""The dispatcher has taken the job off the queue and waits for a slot when
	the job is cancelled: once the slot frees, it doesn't start the job."""
	fake = FakeRedis()
	orch = make_orchestrator(fake, max_concurrent=1)
	orch._slots.acquire()                      # the only slot is busy
	job = QueuedJob()
	enqueue(orch, fake, job)
	assert wait_for(lambda: fake._queue(QUEUE).empty())   # taken: waits for the slot
	orch.cancel(job.job_id)
	assert job.reasons == [jobs.CANCELLED_BEFORE_START]
	orch._slots.release()
	time.sleep(0.3)
	assert not job.ran.is_set()
	assert orch._slots._value == 1


def test_a_job_not_started_is_not_over():
	"""RolloutJob.is_over: false while queued (no thread yet), true once its
	run has ended."""
	options = RolloutOptions(verify=False, verbose=False, webapp=False)
	job = jobs.RolloutJob(uuid.uuid4(), uuid.uuid4(),
	                      RolloutEngine(options, [], ["cmd"]), options)
	assert not job.is_over()
	ended = threading.Event()
	with patch.object(RolloutEngine, "run", return_value=[]):
		job.start(lambda _: ended.set())
		assert ended.wait(5)
	assert wait_for(job.is_over)


# ── KPIs, job status, snapshot expiry ────────────────────────────────────────

def result(status, ip="10.0.0.1", job_id=None, sent=2, verified=None,
           config=None, age_days=0):
	"""A stand-in device result row: its own job unless one is given,
	completed age_days ago."""
	return SimpleNamespace(
		status=status, device_ip=ip, job_id=job_id or uuid.uuid4(),
		commands_sent=sent, commands_verified=verified, fetched_config=config,
		completed_at=dt.datetime.now() - dt.timedelta(days=age_days))


def test_build_kpi():
	"""Four results (1 success) for two devices over three jobs: success rate 25 %,
	3 jobs, 8 commands pushed, the device that failed twice named by its label as the
	top failed - and "device_pushes" 4: it counts device results (pushes, failed
	ones included), not distinct devices, so each device counts once per rollout
	(the Analytics tile says "device push operations")."""
	job = uuid.uuid4()
	rows = [result("success", job_id=job), result("failed", "10.0.0.9", job),
	        result("failed", "10.0.0.9"), result("partial")]
	assert len({r.device_ip for r in rows}) == 2
	kpi = build_kpi(rows, {"10.0.0.9": "edge-9"})
	assert kpi["success_rate"] == 25
	assert kpi["jobs_30d"] == 3
	assert kpi["device_pushes"] == len(rows) == 4
	assert kpi["commands_pushed"] == 8
	assert kpi["top_failed"] == {"ip": "10.0.0.9", "label": "edge-9",
	                             "fail_count": 2}


def test_build_kpi_empty():
	"""Without results the success rate and the top failed device are None."""
	kpi = build_kpi([], {})
	assert kpi["success_rate"] is None and kpi["top_failed"] is None


@pytest.mark.parametrize("statuses,expected", [
	(["success", "success"], "success"),
	(["success", "failed"], "partial"),
	(["success", "partial"], "partial"),
	(["failed", "failed"], "failed"),
	(["success", "cancelled"], "cancelled"),
])
def test_job_status(statuses, expected):
	"""A job's status from its devices': all success → success, any failed or
	partial → partial, all failed → failed, any cancelled → cancelled."""
	assert job_status([result(s) for s in statuses]) == expected


def test_job_status_condition_refuses_an_unknown_status():
	"""job_status_condition has a condition for every status job_status
	gives, and refuses any other (no silent "match nothing")."""
	for status in JOB_STATUSES:
		assert job_status_condition(status) is not None
	with pytest.raises(ValueError):
		job_status_condition("bogus")


class TestConfigExpired:
	DAYS = SETTINGS["config_snapshot_retention_days"].default
	OLD = DAYS + 1

	def test_mismatch_past_window_without_config_is_expired(self):
		"""A verify mismatch older than the snapshot retention, with no config
		left, is reported as expired."""
		assert config_expired(result("partial", verified=1, age_days=self.OLD),
		                      self.DAYS)

	def test_within_window_is_not_expired(self):
		"""A verify mismatch inside the retention window isn't expired."""
		assert not config_expired(result("partial", verified=1, age_days=1),
		                          self.DAYS)

	def test_window_follows_the_setting(self):
		"""The same 3-day-old row is not expired with a 7-day retention and is
		expired with a 2-day one."""
		row = result("partial", verified=1, age_days=3)
		assert not config_expired(row, 7)
		assert config_expired(row, 2)

	def test_config_still_present_is_not_expired(self):
		"""An old mismatch whose config snapshot is still stored isn't expired."""
		assert not config_expired(
			result("partial", verified=1, config="cfg", age_days=self.OLD),
			self.DAYS)

	def test_fully_verified_never_had_a_snapshot(self):
		"""An old fully verified result never had a snapshot, so it isn't expired."""
		assert not config_expired(
			result("success", verified=2, age_days=self.OLD), self.DAYS)

	def test_verify_not_run_never_had_a_snapshot(self):
		"""An old result where verify didn't run never had a snapshot: not expired."""
		assert not config_expired(
			result("success", verified=None, age_days=self.OLD), self.DAYS)


# ── The waiting list for a terminal (python -m src.jobs rollouts) ───────────

def test_rollouts_text_is_empty_without_rollouts():
	"""No rollouts: nothing to print (the scripts take empty output as none)."""
	assert jobs.rollouts_text([]) == ""


def test_rollouts_text_lists_owner_devices_state_started():
	"""Two rollouts: a heading with the count, a header row, then one row each
	with the owner, devices, state and the start (T as a space, '-' for none)."""
	text = jobs.rollouts_text([
		{"user": "alice", "devices": 12, "state": "running", "started": "2026-10-08T14:02:11"},
		{"user": "bob", "devices": 3, "state": "queued", "started": None}])
	lines = text.splitlines()
	assert lines[0] == "2 rollouts running or queued:"
	assert lines[1].split() == ["By", "Devices", "State", "Started"]
	assert lines[2].split() == ["alice", "12", "running", "2026-10-08", "14:02:11"]
	assert lines[3].split() == ["bob", "3", "queued", "-"]
	assert text.endswith("\n")
