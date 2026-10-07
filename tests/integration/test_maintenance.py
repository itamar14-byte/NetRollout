"""Maintenance mode (a database move, src/webapp/maintenance.py): while the
data is copied nothing may write - every route is refused but the few that
don't write, derived from the live url_map so a route added later is covered."""
import pytest
from sqlalchemy import text

from src.orchestration import PAUSED_MESSAGE
from src.webapp.maintenance import IDLE, LOCKED, WAITING
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


def test_health_stays_200_and_says_so(app, locked):
	"""Locked: health still answers 200, with the maintenance state and
	progress."""
	locked.report("Switching")
	resp = app.test_client().get("/_netrollout/health")
	assert resp.status_code == 200
	assert resp.json["maintenance"] == {"state": LOCKED, "progress": "Switching"}


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
	assert app.test_client().get("/_netrollout/health").json["maintenance"]["state"] == WAITING
	before = _audit_rows(app)
	with app.test_request_context("/"):
		app.web.audit("auth.login", username="someone")
	assert _audit_rows(app) == before + 1


def test_locking_waits_for_the_rollouts(app, admin, maintenance, monkeypatch):
	"""lock() refuses (state stays waiting) while the orchestrator isn't idle,
	and locks once it is."""
	assert maintenance.begin(WHAT, admin.id)
	monkeypatch.setattr(app.orchestrator, "idle", lambda: False)
	assert maintenance.lock() is False and maintenance.state == WAITING
	monkeypatch.setattr(app.orchestrator, "idle", lambda: True)
	assert maintenance.lock() and maintenance.state == LOCKED


def test_the_end_resumes_rollouts_and_the_site(app, admin, client_for, locked):
	"""end() goes back to idle: rollouts are accepted, pages served, health
	shows no maintenance."""
	locked.end()
	assert locked.state == IDLE and app.orchestrator.refusal() is None
	assert client_for(admin).get("/dashboard").status_code == 200
	assert app.test_client().get("/_netrollout/health").json["maintenance"] is None
