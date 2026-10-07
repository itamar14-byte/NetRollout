"""Stage 3 (container runtime) through the routes: the health endpoint, the
admin Restart choice, refusing new rollouts while the server drains, the
banner, and sessions following a Server Management Redis switch.

Shutdown.begin is always replaced: the real one exits the process."""
import datetime as dt
import uuid

import pytest
import redis as redis_lib

from src.db.tables import DeviceResult
from src.jobs import DRAINING_MESSAGE, Draining
from src.runtime import VERSION, drain_seconds

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


@pytest.fixture
def begins(app, monkeypatch):
	"""Calls to app.shutdown.begin (never the real one: it exits)."""
	calls = []

	def _begin(deadline, restart):
		calls.append((deadline, restart))
		return len(calls) == 1          # a second request: already in progress
	monkeypatch.setattr(app.shutdown, "begin", _begin)
	return calls


@pytest.fixture
def busy(app, monkeypatch):
	"""A setter for the orchestrator's running / queued rollout counts."""
	def _set(running=0, queued=0):
		monkeypatch.setattr(app.orchestrator, "counts",
		                    lambda: {"running": running, "queued": queued})
	return _set


@pytest.fixture
def draining(app, monkeypatch):
	monkeypatch.setattr(app.orchestrator, "_draining", True)


# ── Health ───────────────────────────────────────────────────────────────────

def test_health_is_public_and_reports_services_counts_version(app, busy):
	"""/_netrollout/health without signing in: 200, no-store, and exactly status ok,
	the version, Postgres and Redis up, the rollout counts, not draining, no
	maintenance."""
	busy(running=1, queued=2)
	resp = app.test_client().get("/_netrollout/health")
	assert resp.status_code == 200
	assert resp.headers["Cache-Control"] == "no-store"
	assert resp.json == {"status": "ok", "version": VERSION, "postgres": True,
	                     "redis": True, "rollouts": {"running": 1, "queued": 2},
	                     "draining": False, "maintenance": None}


def test_health_is_503_when_a_service_is_down(app, monkeypatch):
	"""With Redis down the health endpoint answers 503, status "degraded", redis
	false."""
	monkeypatch.setattr(app.backend, "health",
	                    lambda: {"POSTGRES": True, "REDIS": False})
	resp = app.test_client().get("/_netrollout/health")
	assert resp.status_code == 503
	assert resp.json["status"] == "degraded" and resp.json["redis"] is False


# ── Admin Restart ────────────────────────────────────────────────────────────

def test_restart_when_idle_begins_at_once(admin, client_for, begins, busy):
	"""With no rollouts, an admin's Restart answers 200 and begins one restart,
	with the usual drain deadline (drain_seconds())."""
	busy()
	resp = client_for(admin).post("/admin/server/restart", json={})
	assert resp.status_code == 200
	assert begins == [(drain_seconds(), True)]


def test_restart_with_rollouts_asks_first(admin, client_for, begins, busy):
	"""With rollouts running or queued and no mode chosen, Restart answers 409 with
	the counts and begins nothing."""
	busy(running=2, queued=1)
	resp = client_for(admin).post("/admin/server/restart", json={})
	assert resp.status_code == 409
	assert (resp.json["running"], resp.json["queued"]) == (2, 1)
	assert begins == []


@pytest.mark.parametrize("mode, drains", [("when_finished", True),
                                          ("now", False)])
def test_restart_modes(admin, client_for, begins, busy, mode, drains):
	"""With a rollout running, Restart begins one restart: "when_finished" with the
	drain deadline (drain_seconds()), "now" with 0."""
	busy(running=1)
	resp = client_for(admin).post("/admin/server/restart", json={"mode": mode})
	assert resp.status_code == 200
	((deadline, restart),) = begins
	assert restart is True
	assert deadline == (drain_seconds() if drains else 0)


def test_restart_rejects_unknown_mode_and_a_second_request(
		admin, client_for, begins, busy):
	"""An unknown mode is 422; after a Restart has begun, a second one is 409 with a
	message saying it is already under way."""
	busy()
	c = client_for(admin)
	assert c.post("/admin/server/restart",
	              json={"mode": "later"}).status_code == 422
	assert c.post("/admin/server/restart", json={}).status_code == 200
	again = c.post("/admin/server/restart", json={})
	assert again.status_code == 409 and "already" in again.json["message"]


def test_restart_is_admin_only(make_user, client_for, begins):
	"""An operator's Restart is refused (302 or 403) and begins nothing."""
	resp = client_for(make_user(), xhr=True).post("/admin/server/restart",
	                                               json={})
	assert resp.status_code in (302, 403)
	assert begins == []


# ── While the server drains ──────────────────────────────────────────────────

@pytest.fixture
def operator_device(make_user, make_profile, make_device):
	user = make_user()
	return user, make_device(user, ip="10.0.0.1", profile_id=make_profile(user))


def test_new_rollouts_are_refused_while_draining(operator_device, client_for,
                                                 captured_submits, draining):
	"""While draining, starting a rollout shows the draining message and submits
	nothing."""
	user, device = operator_device
	resp = client_for(user).post("/rollout/start", data={
		"device_ids": [str(device)], "manual_commands": "hostname r1"},
		follow_redirects=True)
	assert DRAINING_MESSAGE.encode() in resp.data.replace(b"&#39;", b"'")
	assert captured_submits == []


def test_rollback_is_refused_while_draining(app, operator_device, client_for,
                                            session_scope, monkeypatch):
	"""A rollback of a finished job, when the orchestrator raises Draining, answers
	503 with the draining message."""
	user, _ = operator_device
	job = uuid.uuid4()
	now = dt.datetime.now()
	with session_scope() as s:
		s.add(DeviceResult(user_id=user.id, job_id=job, started_at=now,
		                   completed_at=now, device_ip="10.0.0.1",
		                   device_port=22, device_type="cisco_ios",
		                   commands_sent=1, status="success"))

	def _refuse(*_, **__):
		raise Draining()
	monkeypatch.setattr(app.orchestrator, "submit", _refuse)
	resp = client_for(user).post(f"/rollout/rollback/{job}",
	                             json={"commands": "no hostname"})
	assert resp.status_code == 503 and resp.json["message"] == DRAINING_MESSAGE


def test_banner_shows_on_every_page_while_draining(
		app, admin, client_for, draining, monkeypatch):
	"""During a Restart's drain, the dashboard and the Server Management page both
	show the drain banner saying NetRollout is restarting."""
	monkeypatch.setattr(app.shutdown, "_restart", True)   # a Restart, not a stop
	for page in ("/dashboard", "/admin/server"):
		html = client_for(admin).get(page).data.decode()
		assert 'id="drainBanner"' in html, page
		assert "NetRollout is restarting." in html


def test_no_banner_normally(admin, client_for):
	"""When not draining, the dashboard has no drain banner."""
	assert 'id="drainBanner"' not in client_for(admin).get("/dashboard") \
		.data.decode()


# ── Sessions follow a Redis switch ───────────────────────────────────────────

def test_sessions_use_the_live_redis_client(app, admin, client_for, redis_url,
                                            monkeypatch):
	"""After backend.redis.client is replaced (a Server Management switch), the
	session store uses the new client and a signed-in page still loads (it used
	to keep the old, closed client and log everyone out)."""
	new_client = redis_lib.Redis.from_url(redis_url)
	monkeypatch.setattr(app.backend.redis, "client", new_client)
	assert app.session_interface.client is new_client
	assert client_for(admin).get("/dashboard").status_code == 200
