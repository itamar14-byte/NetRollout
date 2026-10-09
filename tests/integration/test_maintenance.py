"""Maintenance mode (a database move, src/webapp/db_move.py): while the
data is copied nothing may write - every route is refused but the few that
don't write, derived from the live url_map so a route added later is covered."""
import threading
import uuid

import pytest
from sqlalchemy import text

from src.jobs import PAUSED_MESSAGE
from src.webapp.db_move import MaintenanceState
from tests.integration.test_route_matrix import _routes, _url

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

SERVED_WHILE_LOCKED = {
	"static", "prometheus_metrics",
	"system.health", "system.instance", "system.grafana_auth",
	"admin_servers.database_move_status",      # the moving admin's page follows the move
}
WHAT = "moving to another database"


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


@pytest.fixture
def maintenance(app, monkeypatch):
	# a slip in the gate must not reach the Restart route's exit
	monkeypatch.setattr(app.shutdown, "begin", lambda *a, **k: True)
	yield app.maintenance
	app.maintenance.end()


@pytest.fixture
def locked(maintenance, admin):
	"""Maintenance begun by the admin and locked at once (no rollouts run here)."""
	assert maintenance.begin(WHAT, admin.id)
	assert maintenance.lock()          # the test app runs no rollouts
	return maintenance


def _audit_rows(app):
	with app.backend.postgres.engine.connect() as c:
		return c.execute(text("select count(*) from audit_log")).scalar()


def test_every_route_but_the_read_only_ones_is_refused_while_locked(
		app, admin, client_for, locked):
	"""Locked: every route but SERVED_WHILE_LOCKED answers 503 with Retry-After,
	to a page and to an XHR alike, and nothing is written to the audit log."""
	before = _audit_rows(app)
	page, xhr = client_for(admin), client_for(admin, xhr=True)
	failures = []
	for rule, method in _routes(app):
		if rule.endpoint in SERVED_WHILE_LOCKED:
			continue
		for client in (page, xhr):
			resp = client.open(_url(rule), method=method)
			if resp.status_code != 503 or resp.headers.get("Retry-After") is None:
				failures.append(f"{method} {rule.rule} -> {resp.status_code}")
	assert not failures, "\n".join(failures)
	assert _audit_rows(app) == before


def test_signed_out_people_get_it_too(app, client_for, locked):
	"""Locked: the sign-in page and a sign-in attempt get 503 ("under
	maintenance") too."""
	resp = client_for().get("/")
	assert resp.status_code == 503 and b"under maintenance" in resp.data
	assert client_for().post("/login", data={"username": "admin", "password": "x"}).status_code == 503


def test_pages_get_the_maintenance_page_and_requests_json(admin, client_for, locked):
	"""Locked: a page gets the maintenance page with the progress text, a
	request gets 503 JSON with maintenance true and what is going on."""
	locked.report("Copying the data - audit_log")
	page = client_for(admin).get("/dashboard")
	assert page.status_code == 503
	assert b"under maintenance" in page.data and b"Copying the data - audit_log" in page.data
	call = client_for(admin, xhr=True).post("/admin/settings", json={})
	assert call.status_code == 503 and call.json["maintenance"] is True
	assert WHAT in call.json["message"]


def test_a_live_log_stream_gets_json_while_locked(admin, client_for, locked):
	"""Locked: the live log's stream (a background request by its path, as the session
	rules count it) gets the 503 JSON answer like the page's other background requests,
	not the maintenance page meant for a person."""
	resp = client_for(admin).get(f"/rollout/stream/{uuid.uuid4()}")
	assert resp.status_code == 503 and resp.json["maintenance"] is True


def test_health_stays_200_and_says_so(app, locked):
	"""Locked: health still answers 200, with the maintenance state and
	progress."""
	locked.report("Switching")
	resp = app.test_client().get("/_netrollout/health")
	assert resp.status_code == 200
	assert resp.json["maintenance"] == {"state": MaintenanceState.LOCKED, "progress": "Switching"}


def test_nothing_is_audited_while_locked(app, admin, locked):
	"""Locked: web.audit writes no audit row."""
	before = _audit_rows(app)
	with app.test_request_context("/"):
		app.web.audit("auth.login", username="someone")
	assert _audit_rows(app) == before


def test_waiting_pauses_rollouts_shows_a_banner_and_writes_as_usual(
		app, admin, client_for, maintenance):
	"""Waiting: only one maintenance at a time, rollouts are refused with
	PAUSED_MESSAGE, pages show the banner, health says waiting, and audit
	rows are still written."""
	assert maintenance.begin(WHAT, admin.id)
	assert maintenance.begin(WHAT, admin.id) is False          # one at a time
	assert app.orchestrator.refusal() == PAUSED_MESSAGE
	page = client_for(admin).get("/admin/users")
	assert page.status_code == 200 and b'id="maintenanceBanner"' in page.data
	assert app.test_client().get("/_netrollout/health").json["maintenance"]["state"] == MaintenanceState.WAITING
	before = _audit_rows(app)
	with app.test_request_context("/"):
		app.web.audit("auth.login", username="someone")
	assert _audit_rows(app) == before + 1


def test_locking_waits_for_the_rollouts(app, admin, maintenance, monkeypatch):
	"""lock() refuses (state stays waiting) while the orchestrator isn't idle,
	and locks once it is."""
	assert maintenance.begin(WHAT, admin.id)
	monkeypatch.setattr(app.orchestrator, "idle", lambda: False)
	assert maintenance.lock() is False and maintenance.state == MaintenanceState.WAITING
	monkeypatch.setattr(app.orchestrator, "idle", lambda: True)
	assert maintenance.lock() and maintenance.state == MaintenanceState.LOCKED


def _held_in(app, monkeypatch, endpoint, fail=False):
	"""The view of `endpoint` replaced by one that waits (until released) and then
	answers - or raises, with fail. :returns: (entered, release) events"""
	entered, release = threading.Event(), threading.Event()

	def held(*args, **kwargs):
		entered.set()
		assert release.wait(10)
		if fail:
			raise RuntimeError("the view failed")
		return "done"
	monkeypatch.setitem(app.view_functions, endpoint, held)
	return entered, release


def _in_thread(call):
	thread = threading.Thread(target=lambda: _quietly(call), daemon=True)
	thread.start()
	return thread


def _quietly(call):
	try:
		call()
	except RuntimeError:
		pass        # the failing view (testing mode raises it in the client)


@pytest.mark.parametrize("fail", [False, True])
def test_the_lock_waits_for_a_request_under_way(app, admin, client_for, maintenance,
                                                monkeypatch, fail):
	"""A request under way (begun before maintenance) keeps the lock from coming:
	lock() refuses, waiting, until it has ended - answered or failed - and locks
	then."""
	entered, release = _held_in(app, monkeypatch, "jobs.dashboard", fail)
	client = client_for(admin)
	thread = _in_thread(lambda: client.get("/dashboard"))
	assert entered.wait(10)
	assert maintenance.begin(WHAT, admin.id)
	assert maintenance.lock() is False and maintenance.state == MaintenanceState.WAITING
	release.set()
	thread.join(10)
	assert maintenance.lock() and maintenance.state == MaintenanceState.LOCKED


def test_a_live_log_open_doesnt_hold_the_lock(app, admin, client_for, maintenance, monkeypatch):
	"""The live log's stream (open for a whole rollout, and it doesn't write) isn't
	waited for: maintenance locks while one is open."""
	entered, release = _held_in(app, monkeypatch, "rollout.rollout_stream")
	client = client_for(admin)
	thread = _in_thread(lambda: client.get(f"/rollout/stream/{uuid.uuid4()}"))
	assert entered.wait(10)
	try:
		assert maintenance.begin(WHAT, admin.id)
		assert maintenance.lock() and maintenance.state == MaintenanceState.LOCKED
	finally:
		release.set()
		thread.join(10)


def test_a_refused_request_isnt_waited_for(app, admin, client_for, locked):
	"""A request refused while locked (503) isn't counted as under way: after the
	maintenance ends, the next one locks at once."""
	assert client_for(admin).get("/dashboard").status_code == 503
	locked.end()
	assert locked.begin(WHAT, admin.id)
	assert locked.lock()


def test_the_end_resumes_rollouts_and_the_site(app, admin, client_for, locked):
	"""end() goes back to idle: rollouts are accepted, pages served, health
	shows no maintenance."""
	locked.end()
	assert locked.state == MaintenanceState.IDLE and app.orchestrator.refusal() is None
	assert client_for(admin).get("/dashboard").status_code == 200
	assert app.test_client().get("/_netrollout/health").json["maintenance"] is None
