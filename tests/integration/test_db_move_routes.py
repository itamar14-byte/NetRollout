"""Server Management -> Database: the move's routes (the move itself, on real
databases, is test_db_move.py)."""
import time
import uuid

import pytest
from sqlalchemy import create_engine, make_url, text

from src.db import move
from src.db.connections import PostgresConfig
from src.db.tables import AuditLog, User
from src.webapp import db_move
from tests.integration.conftest import PG_ADMIN_URL
from tests.integration.test_db_move import (_wait, holding_netrollout, mover,  # noqa: F401 - fixtures
                                            target)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def test_the_page_shows_the_dbas_way_first(admin, client_for):
	"""Server Management shows the DBA's way pressed before the administrator-login
	way, with what to ask for, and no Move back on the bundled database."""
	page = client_for(admin).get("/admin/server")
	assert page.status_code == 200
	html = page.data.decode()
	assert "Ask your database administrator for:" in html
	assert html.index('id="mvWayDba" aria-pressed="true"') < html.index('id="mvWayAdmin" aria-pressed="false"')
	assert "no superuser, no CREATEDB, no CREATEROLE" in html
	assert 'id="mvBack"' not in html            # on the bundled database: nothing to move back to


def test_the_sql_comes_with_a_new_password_each_time(admin, client_for, monkeypatch):
	"""Each SQL request gets a new password, written into the SQL, plus Grafana's
	known password and the four access needs."""
	monkeypatch.setenv("GRAFANA_DB_PASSWORD", "gr-secret")
	c = client_for(admin, xhr=True)
	first = c.post("/admin/server/database/sql", json={"database": "ops", "schema": "nr", "login": "nr_app"}).json
	second = c.post("/admin/server/database/sql", json={"database": "ops", "schema": "nr", "login": "nr_app"}).json
	assert first["status"] == "ok" and first["password"] != second["password"]
	assert f"""CREATE ROLE "nr_app" LOGIN PASSWORD '{first["password"]}';""" in first["sql"]
	assert "CREATE ROLE grafana_reader LOGIN PASSWORD 'gr-secret';" in first["sql"]
	assert first["grafana_known"] is True and len(first["access"]) == 4


def test_check_needs_every_field(admin, client_for):
	"""A check with only the host given answers 422."""
	resp = client_for(admin, xhr=True).post("/admin/server/database/check", json={"host": "db1"})
	assert resp.status_code == 422


@pytest.mark.parametrize("url", ["/admin/server/database/sql", "/admin/server/database/prepare",
                                 "/admin/server/database/check", "/admin/server/database/move"])
def test_a_schema_name_netrollout_cant_use_is_refused_by_every_step(admin, client_for, url):
	"""The SQL, prepare, check and move each answer 422 for a schema name that isn't a
	plain lowercase name (here with a space and a capital), saying the rule."""
	resp = client_for(admin, xhr=True).post(url, json={
		"host": "127.0.0.1", "port": "1", "database": "ops", "schema": "Net Rollout",
		"user": "x", "password": "y", "admin_user": "postgres", "admin_password": "z",
		"login": "nr_app"})
	assert resp.status_code == 422, resp.json
	assert resp.json["message"] == ('The schema name "Net Rollout" can\'t be used: lowercase '
	                                'letters, digits and _ only, starting with a letter or _, '
	                                'at most 63 characters.')


def test_check_refuses_the_current_database(app, admin, client_for):
	"""A check of the database NetRollout uses now is refused as such."""
	url = make_url(app.backend.postgres.config.get_url())
	resp = client_for(admin, xhr=True).post("/admin/server/database/check", json={
		"host": url.host, "port": str(url.port), "database": url.database,
		"user": "x", "password": "y"})
	assert resp.json["status"] == "error" and "uses now" in resp.json["message"]


def test_check_reports_an_unreachable_server(admin, client_for):
	"""A check of a server nobody listens on reports not ok, "Couldn't connect" first."""
	resp = client_for(admin, xhr=True).post("/admin/server/database/check", json={
		"host": "127.0.0.1", "port": "1", "database": "x", "user": "x", "password": "y"})
	report = resp.json["report"]
	assert report["ok"] is False and report["problems"][0].startswith("Couldn't connect")


def test_nothing_to_move_back_to_on_the_bundled_database(admin, client_for):
	"""Move back while on the bundled database answers 409."""
	resp = client_for(admin, xhr=True).post("/admin/server/database/move-back")
	assert resp.status_code == 409


def test_status_while_waiting_lists_the_rollouts_by_name(app, admin, client_for, monkeypatch):
	"""While the move waits, its status gives whole seconds left (no raw deadline)
	and each running rollout with its user's name and device count."""
	monkeypatch.setattr(app.db_move, "status", lambda: {"state": db_move.WAITING, "deadline": 0})
	monkeypatch.setattr(app.db_move, "seconds_left", lambda: 90.4)
	monkeypatch.setattr(app.orchestrator, "jobs", lambda: [
		{"job_id": admin.id, "user_id": admin.id, "devices": 3, "state": "running", "started": None}])
	move = client_for(admin, xhr=True).get("/admin/server/database/move/status").json["move"]
	assert move["seconds_left"] == 90 and "deadline" not in move
	assert move["rollouts"][0]["user"] == admin.username and move["rollouts"][0]["devices"] == 3


def test_cancel_only_while_waiting(admin, client_for):
	"""Cancel the move with no move waiting answers 409."""
	assert client_for(admin, xhr=True).post("/admin/server/database/move/cancel").status_code == 409


def test_redis_switch_is_refused_while_rollouts_run(app, admin, client_for, monkeypatch):
	"""A Redis switch with a rollout running answers 409 "Rollouts are running"."""
	monkeypatch.setattr(app.orchestrator, "counts", lambda: {"running": 1, "queued": 0})
	resp = client_for(admin, xhr=True).post("/admin/server/redis/save", json={
		"host": "cache.example.org", "port": "6380"})
	assert resp.status_code == 409 and "Rollouts are running" in resp.json["message"]


def test_operators_get_none_of_it(make_user, client_for):
	"""An operator posting to sql, check, move or move-back gets 302 or 403."""
	c = client_for(make_user(), xhr=True)
	for url in ("/admin/server/database/sql", "/admin/server/database/check",
	            "/admin/server/database/move", "/admin/server/database/move-back"):
		assert c.post(url, json={}).status_code in (302, 403)


def test_prepare_needs_the_administrator_login(admin, client_for):
	"""The administrator-login way without the administrator's login answers 422
	and prepares nothing."""
	resp = client_for(admin, xhr=True).post("/admin/server/database/prepare", json={
		"host": "127.0.0.1", "port": "1", "database": "ops"})
	assert resp.status_code == 422


def test_prepare_on_an_unreachable_server_fails_and_is_audited(admin, client_for, session_scope):
	"""Preparing on a server nobody listens on answers the error, and the failure
	is audited (database.prepare_failed) - never the administrator's password."""
	resp = client_for(admin, xhr=True).post("/admin/server/database/prepare", json={
		"host": "127.0.0.1", "port": "1", "admin_user": "postgres", "admin_password": "s3cret",
		"database": "ops", "schema": "nr", "login": "nr_app"})
	assert resp.status_code == 400 and resp.json["status"] == "error"
	with session_scope() as s:
		rows = s.query(AuditLog).filter_by(action="database.prepare_failed").all()
		assert len(rows) == 1 and rows[0].success is False
		assert "s3cret" not in str(rows[0].detail)


def test_a_move_to_an_unreachable_server_is_refused(admin, client_for):
	"""A move to a server nobody listens on is refused (409: it's checked first)
	and no move begins."""
	c = client_for(admin, xhr=True)
	resp = c.post("/admin/server/database/move", json={
		"host": "127.0.0.1", "port": "1", "database": "x", "user": "x", "password": "y"})
	assert resp.status_code == 409
	assert c.get("/admin/server/database/move/status").json["move"]["state"] == "idle"


# ── the success paths, on real scratch databases (test_db_move.py's fixtures) ──

def _form(config):
	"""The move form for a PostgresConfig, as the page posts it."""
	return {"host": config.host, "port": str(config.port), "database": config.database,
	        "user": config.user, "password": config.password, "schema": config.schema or ""}


def test_prepare_creates_what_the_move_needs_and_is_audited(admin, client_for, session_scope,
                                                            monkeypatch):
	"""The administrator-login way creates the login, the database and its schema: 200
	with the plan and a password for the new login that works (the target then passes
	the check, empty); database.prepared is audited with the login, schema and what was
	done - never the administrator's password."""
	monkeypatch.delenv("GRAFANA_DB_PASSWORD", raising=False)
	server = make_url(PG_ADMIN_URL)
	port = str(server.port or 5432)
	suffix = uuid.uuid4().hex[:8]
	database, login = f"rollout_prep_{suffix}", f"nr_prep_{suffix}"
	try:
		resp = client_for(admin, xhr=True).post("/admin/server/database/prepare", json={
			"host": server.host, "port": port,
			"admin_user": server.username, "admin_password": server.password,
			"database": database, "schema": "nr", "login": login})
		assert resp.status_code == 200, resp.json
		body = resp.json
		assert (body["database"], body["schema"], body["login"]) == (database, "nr", login)
		assert body["grafana_known"] is False and body["password"]
		assert body["done"][0] == f"login {login} created"
		assert f"database {database} created" in body["done"] and "schema nr created" in body["done"]
		report = move.check_target(PostgresConfig(host=server.host, port=port, database=database,
		                                          user=login, password=body["password"],
		                                          schema="nr"))
		assert report.ok, report.problems
		assert report.contents == move.EMPTY
		with session_scope() as s:
			(row,) = s.query(AuditLog).filter_by(action="database.prepared").all()
			assert row.success is True
			assert row.object_label == f"{server.host}:{port}/{database}"
			assert row.detail == {"login": login, "schema": "nr", "done": body["done"]}
			assert server.password not in str(row.detail)
	finally:
		with create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT").connect() as c:
			c.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
			c.execute(text(f'DROP ROLE IF EXISTS "{login}"'))


def test_a_move_through_the_page_and_back(app, admin, client_for, target, mover, monkeypatch):
	"""Move (the form) answers 200 with the move under way to the target; it ends done
	on the target, the admin's account there. Move back answers 200 with the move back
	to the bundled database under way, and it ends there."""
	monkeypatch.setattr(app, "db_move", mover)
	home = app.backend.postgres.config
	c = client_for(admin, xhr=True)

	resp = c.post("/admin/server/database/move", json=_form(target))
	assert resp.status_code == 200, resp.json
	started = resp.json["move"]
	assert started["target"] == db_move.describe(target) and started["back"] is False
	assert started["state"] in (db_move.WAITING, db_move.COPYING, db_move.SWITCHING, db_move.DONE)
	assert _wait(mover)["state"] == db_move.DONE
	assert app.backend.postgres.config == target
	with app.backend.postgres.get_session() as s:
		assert s.query(User).filter_by(id=admin.id).one().username == admin.username

	resp = c.post("/admin/server/database/move-back")
	assert resp.status_code == 200, (resp.headers.get("Location"), resp.json)
	assert resp.json["move"]["back"] is True
	assert resp.json["move"]["target"] == db_move.describe(app.backend.bundled_postgres())
	assert _wait(mover)["state"] == db_move.DONE
	assert db_move.same_database(app.backend.postgres.config, home)


def test_a_netrollout_database_there_needs_replace_ticked(app, admin, client_for, target, mover,
                                                         monkeypatch):
	"""Move to a target holding a NetRollout database answers 409 ("tick 'replace'")
	without replace, or with replace not true (the string "true"), and no move
	begins; with replace true it answers 200 and the move ends done."""
	monkeypatch.setattr(app, "db_move", mover)
	holding_netrollout(target)
	c = client_for(admin, xhr=True)
	for body in (_form(target), {**_form(target), "replace": False},
	             {**_form(target), "replace": "true"}):
		resp = c.post("/admin/server/database/move", json=body)
		assert resp.status_code == 409, resp.json
		assert "tick 'replace' to overwrite it" in resp.json["message"]
		assert mover.status()["state"] == db_move.IDLE
	resp = c.post("/admin/server/database/move", json={**_form(target), "replace": True})
	assert resp.status_code == 200, resp.json
	assert _wait(mover)["state"] == db_move.DONE


def test_the_move_started_audit_row_survives_the_move(app, admin, client_for, target, mover,
                                                     monkeypatch):
	"""database.move_started (written by the route once the move has begun) is in the
	database NetRollout ends up on, naming the target, back False - also when the
	move's thread locks maintenance before the route writes the row (forced here so
	the order is certain; with no rollout running it is the usual order)."""
	monkeypatch.setattr(app, "db_move", mover)
	start = mover.start

	def start_then_locked(*args, **kwargs):
		start(*args, **kwargs)
		end = time.time() + 10
		while not app.maintenance.writes_blocked and mover.running and time.time() < end:
			time.sleep(0.01)
	monkeypatch.setattr(mover, "start", start_then_locked)

	resp = client_for(admin, xhr=True).post("/admin/server/database/move", json=_form(target))
	assert resp.status_code == 200, resp.json
	assert _wait(mover)["state"] == db_move.DONE
	with app.backend.postgres.get_session() as s:
		rows = [(r.object_label, r.detail) for r in
		        s.query(AuditLog).filter_by(action="database.move_started")]
	assert rows == [(db_move.describe(target), {"back": False})]
