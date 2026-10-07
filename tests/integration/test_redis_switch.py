"""A Redis switch is live - no restart: on the real Redis, from the test
database (15, the "bundled" one here) to another (13) and back."""
import threading
import time
import uuid

import pytest
import redis as redis_lib
from dotenv import dotenv_values

from src.db.backend import BUNDLED_REDIS_KEY
from src.job_store import JobStore

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

OTHER_DB = 13


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


@pytest.fixture
def other_redis(app):
	"""Redis database 13 on the same server, emptied; the app always put back
	on the test database and runtime.env restored."""
	home = app.backend.redis.config
	url = home.get_url().rsplit("/", 1)[0] + f"/{OTHER_DB}"
	other = redis_lib.from_url(url)
	other.flushdb()
	runtime_env = app.backend._CONFIG_ENV
	saved = runtime_env.read_text() if runtime_env.exists() else None
	runtime_env.unlink(missing_ok=True)
	yield other
	if app.backend.redis.config.place() != home.place():
		app.backend.redis.reload_db(home)
	other.flushdb()
	other.close()
	if saved is None:
		runtime_env.unlink(missing_ok=True)
	else:
		runtime_env.write_text(saved)


class StubJob:
	"""Just enough of a RolloutJob for the dispatcher to start it."""
	def __init__(self, user_id):
		self.job_id, self.user_id, self.started_at = uuid.uuid4(), user_id, None
		self.begun = threading.Event()

	def start(self, cleanup):
		self.begun.set()


def test_switch_there_and_back_without_a_restart(app, admin, client_for, make_user, other_redis):
	"""Switching to db 13 works live: the mode turns external and runtime.env
	remembers the bundled Redis; the switching admin stays signed in (session and
	its index moved), an operator is signed out, and the dispatcher takes a job
	from the new Redis. Switching back restores the place and the bundled mode
	and clears a session left in the bundled Redis from before."""
	parts = app.backend.redis.config.place()
	before = client_for(admin)
	operator = client_for(make_user())
	assert operator.get("/dashboard").status_code == 200        # signed in, before the switch
	switched = before.post("/admin/server/redis/save", json={
		"host": parts[0], "port": str(parts[1]), "db": str(OTHER_DB),
		"password": app.backend.redis.config.get_url().split(":")[2].split("@")[0]})
	assert switched.json["status"] == "ok", switched.json
	assert app.backend.redis.config.place()[2] == OTHER_DB
	assert app.backend.connection_modes()["REDIS"] == "external"     # same host, not the bundled one
	assert BUNDLED_REDIS_KEY in dotenv_values(app.backend._CONFIG_ENV)

	# the admin who switched stays signed in - the session went along, and
	# its index entry (Live Sessions, Terminate Session) too
	assert before.get("/admin/server").status_code == 200
	sid = other_redis.get(f"user_session:{admin.id}")
	assert sid and other_redis.get(f"redis_session:{sid.decode()}")
	# anyone else's session stayed in the Redis left behind: signed out
	assert operator.get("/dashboard").status_code == 302
	after = before

	# the rollout dispatcher takes its queue from the new Redis (it re-reads
	# the client after each wait)
	job = StubJob(admin.id)
	with app.orchestrator._lock:
		app.orchestrator._jobs[job.job_id] = job
	JobStore(app.backend.redis).enqueue(job.job_id)
	try:
		assert job.begun.wait(15), "the dispatcher didn't take the job from the new Redis"
	finally:
		with app.orchestrator._lock:
			app.orchestrator._jobs.pop(job.job_id, None)
		app.orchestrator._slots.release()

	# a session left in the bundled Redis from before must not come back
	bundled = redis_lib.from_url(app.backend.bundled_redis().get_url())
	bundled.set("redis_session:from-before", b"x")
	back = after.post("/admin/server/redis/back")
	assert back.json["status"] == "ok", back.json
	assert app.backend.redis.config.place() == parts
	assert app.backend.connection_modes()["REDIS"] == "bundled"
	assert bundled.get("redis_session:from-before") is None
	bundled.close()


def test_refused_while_rollouts_run(app, admin, client_for, monkeypatch, other_redis):
	"""Switch back with a rollout running (on the bundled Redis) answers 409."""
	monkeypatch.setattr(app.orchestrator, "counts", lambda: {"running": 1, "queued": 0})
	resp = client_for(admin, xhr=True).post("/admin/server/redis/back")
	assert resp.status_code in (409,)


def test_nothing_to_switch_back_to_on_the_bundled_redis(admin, client_for, other_redis):
	"""Switch back while on the bundled Redis answers 409: nothing to go back to."""
	assert client_for(admin, xhr=True).post("/admin/server/redis/back").status_code == 409
