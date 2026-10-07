"""src/job_store.py against a real Redis: a job's life in its keys, and the
startup reset of what a crash leaves behind."""
import uuid

import pytest

from src.job_store import JobStore
from src.webapp.setup import clear_stale_jobs

pytestmark = pytest.mark.redis


@pytest.fixture
def store(app):
	s = JobStore(app.backend.redis)
	s.reset_stale()
	yield s
	s.reset_stale()


def test_a_jobs_life(store):
	"""A job's keys through add → started → finished: pending and counted as
	queued, listed for its user and overall, then active, then gone with the
	counters back at (0, 0)."""
	job, user = uuid.uuid4(), uuid.uuid4()
	store.add(job, user, device_count=3)
	assert store.meta(job)["status"] == "pending" and store.counts() == (0, 1)
	assert store.job_ids(user) == [str(job)] and store.job_ids() == [str(job)]
	# (the queue isn't checked here: the test app's own dispatcher takes
	# whatever is queued — tests/unit/test_orchestration.py covers it)
	store.started(job)
	assert store.meta(job)["status"] == "active" and store.counts() == (1, 0)
	store.finished(job, user, was_running=True)
	assert store.meta(job) == {} and store.job_ids(user) == []
	assert store.counts() == (0, 0)


def test_a_crash_leaves_nothing_after_the_next_start(app, store, capsys):
	"""Two started jobs left by a killed process are cleared by the startup
	reset (clear_stale_jobs), which says so; no job ids or counts remain."""
	user = uuid.uuid4()
	for _ in range(2):               # what a killed process leaves behind
		job = uuid.uuid4()
		store.add(job, user, device_count=1)
		store.started(job)
	clear_stale_jobs(app.backend.redis)
	assert "Cleared 2 rollout(s) left over" in capsys.readouterr().out
	assert store.job_ids() == [] and store.job_ids(user) == []
	assert store.counts() == (0, 0)
