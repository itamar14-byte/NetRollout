"""Server Management -> Database: the move's routes (the move itself, on real
databases, is test_db_move.py)."""
import pytest

from src.webapp import db_move

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def test_the_page_shows_the_dbas_way_first(admin, client_for):
	page = client_for(admin).get("/admin/server")
	assert page.status_code == 200
	html = page.data.decode()
	assert "Ask your database administrator for:" in html
	assert html.index('id="mvWayDba" aria-pressed="true"') < html.index('id="mvWayAdmin" aria-pressed="false"')
	assert "no superuser, no CREATEDB, no CREATEROLE" in html
	assert 'id="mvBack"' not in html            # on the bundled database: nothing to move back to


def test_the_sql_comes_with_a_new_password_each_time(admin, client_for, monkeypatch):
	monkeypatch.setenv("GRAFANA_DB_PASSWORD", "gr-secret")
	c = client_for(admin, xhr=True)
	first = c.post("/admin/server/database/sql", json={"database": "ops", "schema": "nr", "login": "nr_app"}).json
	second = c.post("/admin/server/database/sql", json={"database": "ops", "schema": "nr", "login": "nr_app"}).json
	assert first["status"] == "ok" and first["password"] != second["password"]
	assert f"""CREATE ROLE "nr_app" LOGIN PASSWORD '{first["password"]}';""" in first["sql"]
	assert "CREATE ROLE grafana_reader LOGIN PASSWORD 'gr-secret';" in first["sql"]
	assert first["grafana_known"] is True and len(first["access"]) == 4


def test_check_needs_every_field(admin, client_for):
	resp = client_for(admin, xhr=True).post("/admin/server/database/check", json={"host": "db1"})
	assert resp.status_code == 422


def test_check_refuses_the_current_database(app, admin, client_for):
	from sqlalchemy import make_url
	url = make_url(app.backend.postgres.config.get_url())
	resp = client_for(admin, xhr=True).post("/admin/server/database/check", json={
		"host": url.host, "port": str(url.port), "database": url.database,
		"user": "x", "password": "y"})
	assert resp.json["status"] == "error" and "uses now" in resp.json["message"]


def test_check_reports_an_unreachable_server(admin, client_for):
	resp = client_for(admin, xhr=True).post("/admin/server/database/check", json={
		"host": "127.0.0.1", "port": "1", "database": "x", "user": "x", "password": "y"})
	report = resp.json["report"]
	assert report["ok"] is False and report["problems"][0].startswith("Couldn't connect")


def test_nothing_to_move_back_to_on_the_bundled_database(admin, client_for):
	resp = client_for(admin, xhr=True).post("/admin/server/database/move-back")
	assert resp.status_code == 409


def test_status_while_waiting_lists_the_rollouts_by_name(app, admin, client_for, monkeypatch):
	monkeypatch.setattr(app.db_move, "status", lambda: {"state": db_move.WAITING, "deadline": 0})
	monkeypatch.setattr(app.db_move, "seconds_left", lambda: 90.4)
	monkeypatch.setattr(app.orchestrator, "jobs", lambda: [
		{"job_id": admin.id, "user_id": admin.id, "devices": 3, "state": "running", "started": None}])
	move = client_for(admin, xhr=True).get("/admin/server/database/move/status").json["move"]
	assert move["seconds_left"] == 90 and "deadline" not in move
	assert move["rollouts"][0]["user"] == admin.username and move["rollouts"][0]["devices"] == 3


def test_cancel_only_while_waiting(admin, client_for):
	assert client_for(admin, xhr=True).post("/admin/server/database/move/cancel").status_code == 409


def test_redis_switch_is_refused_while_rollouts_run(app, admin, client_for, monkeypatch):
	monkeypatch.setattr(app.orchestrator, "counts", lambda: {"running": 1, "queued": 0})
	resp = client_for(admin, xhr=True).post("/admin/server/redis/save", json={
		"host": "cache.example.org", "port": "6380"})
	assert resp.status_code == 409 and "Rollouts are running" in resp.json["message"]


def test_operators_get_none_of_it(make_user, client_for):
	c = client_for(make_user(), xhr=True)
	for url in ("/admin/server/database/sql", "/admin/server/database/check",
	            "/admin/server/database/move", "/admin/server/database/move-back"):
		assert c.post(url, json={}).status_code in (302, 403)
