"""src/jobs.py against a real Redis: a job's life in its keys, and the
startup reset of what a crash leaves behind."""
import json
import uuid

import pytest

from src import jobs
from src.jobs import JobStore, clear_stale_jobs

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
	# whatever is queued — tests/unit/test_jobs.py covers it)
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


# ── The host's view: JobStore.rollouts, with_owners, python -m src.jobs ─────

def _two_jobs(store, owner):
	"""A running job of `owner` (5 devices), then a queued one of a user id that
	isn't in the database (2 devices). :returns: their ids"""
	running, queued = uuid.uuid4(), uuid.uuid4()
	store.add(running, owner, device_count=5)
	store.started(running)
	store.add(queued, uuid.uuid4(), device_count=2)
	return running, queued


@pytest.mark.postgres
def test_rollouts_lists_each_job_oldest_first(store, make_user):
	"""JobStore.rollouts gives each job's id, owner id, devices, state (queued /
	running / cancelling) and start to the second (None while queued), oldest
	first."""
	owner = make_user()
	running, queued = _two_jobs(store, owner.id)
	rows = store.rollouts()
	assert [r["job_id"] for r in rows] == [str(running), str(queued)]
	assert rows[0]["user_id"] == str(owner.id) and rows[0]["devices"] == 5
	assert rows[0]["state"] == "running" and len(rows[0]["started"]) == 19
	assert rows[1]["state"] == "queued" and rows[1]["started"] is None
	store.set_status(running, "cancelling")
	assert store.rollouts()[0]["state"] == "cancelling"


@pytest.mark.postgres
def test_with_owners_names_each_rollout(app, make_user):
	"""with_owners adds the owner's username for a UUID or a text id, "?" for an
	id without a user, and gives the ids as text."""
	alice, bob = make_user(), make_user()
	rows = jobs.with_owners([
		{"job_id": uuid.UUID(int=1), "user_id": alice.id, "devices": 1, "state": "running", "started": None},
		{"job_id": "j2", "user_id": str(bob.id), "devices": 2, "state": "queued", "started": None},
		{"job_id": "j3", "user_id": uuid.uuid4(), "devices": 3, "state": "queued", "started": None}],
		app.backend.postgres)
	assert [r["user"] for r in rows] == [alice.username, bob.username, "?"]
	assert rows[0]["job_id"] == str(uuid.UUID(int=1)) and rows[0]["user_id"] == str(alice.id)
	assert rows[2]["devices"] == 3


@pytest.fixture
def as_the_container(test_db_url, redis_url, tmp_path, monkeypatch):
	"""python -m src.jobs's view: the test database and Redis in the environment,
	a NetRollout home without a runtime.env (never the developer's)."""
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	monkeypatch.setenv("DATABASE_URL", test_db_url)
	monkeypatch.setenv("REDIS_URL", redis_url)


@pytest.mark.postgres
def test_the_command_lists_the_rollouts_as_json(store, make_user, as_the_container, capsys):
	"""`python -m src.jobs rollouts --json` prints one line {"rollouts": [...]}:
	each rollout's id, owner name ("?" unknown), devices and state; exit 0."""
	owner = make_user()
	running, queued = _two_jobs(store, owner.id)
	assert jobs.main(["rollouts", "--json"]) == 0
	listed = json.loads(capsys.readouterr().out)["rollouts"]
	assert [(r["job_id"], r["user"], r["devices"], r["state"]) for r in listed] == [
		(str(running), owner.username, 5, "running"), (str(queued), "?", 2, "queued")]


@pytest.mark.postgres
def test_the_command_prints_a_table_or_nothing(store, make_user, as_the_container, capsys):
	"""`rollouts` prints nothing when no rollout runs, else the table with the
	owner's name; exit 0 both times."""
	assert jobs.main(["rollouts"]) == 0
	assert capsys.readouterr().out == ""
	owner = make_user()
	_two_jobs(store, owner.id)
	assert jobs.main(["rollouts"]) == 0
	out = capsys.readouterr().out
	assert out.startswith("2 rollouts running or queued:") and owner.username in out


def test_the_command_asks_the_stop_to_cancel_now(store, as_the_container, capsys):
	"""`stop-now` sets the mark the drain looks for (JobStore.stop_now_requested)
	and says what happens; exit 0."""
	assert not store.stop_now_requested()
	assert jobs.main(["stop-now"]) == 0
	assert store.stop_now_requested()
	assert "cancelled" in capsys.readouterr().out
