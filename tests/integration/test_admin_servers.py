"""Server Management: the page (an unreachable Redis shown as disconnected),
the Redis test, and the save routes (the connection swap stubbed out). The
database move routes are in test_db_move_routes.py, the certificate in
test_certificate.py."""
import pytest
from dotenv import dotenv_values
from redis.exceptions import TimeoutError as RedisTimeoutError

from tests.integration.test_admin_users import admin  # noqa: F401 - fixture


# ── Server management ────────────────────────────────────────────────────────

def test_server_page_shows_an_unreachable_redis_as_disconnected(
		app, admin, client_for, monkeypatch):
	"""A Redis host that doesn't answer (a timeout, not a refusal) shows as
	Disconnected on Server Management - the page itself still opens."""
	def no_answer():
		raise RedisTimeoutError("Timeout connecting to server")
	client = client_for(admin)
	monkeypatch.setattr(app.backend.redis.client, "ping", no_answer)
	resp = client.get("/admin/server")
	assert resp.status_code == 200
	redis_card = resp.get_data(as_text=True).split(">Redis<", 1)[1]
	assert "Disconnected" in redis_card.split("mode-badge", 1)[0]


def test_server_page_renders(admin, client_for):
	"""Server Management renders for an admin."""
	assert client_for(admin).get("/admin/server").status_code == 200


def test_redis_test_endpoint(admin, client_for):
	"""Testing a Redis address where nothing listens answers status error."""
	resp = client_for(admin).post("/admin/server/redis/test", json={
		"host": "127.0.0.1", "port": "6999"})
	assert resp.json["status"] == "error"


@pytest.fixture
def switch_writes_only(app, monkeypatch):
	"""A save swaps the shared app's connection; stub only the swap, so the
	route → backend → config/runtime.env path runs for real. Yields the
	runtime.env path, removed before and after."""
	monkeypatch.setattr(app.backend.redis, "reload_db", lambda config: None)
	runtime_env = app.backend.env.path
	runtime_env.unlink(missing_ok=True)
	yield runtime_env
	runtime_env.unlink(missing_ok=True)


def test_save_routes_write_runtime_env(app, admin, client_for, switch_writes_only):
	"""Saving another Redis writes its keys to config/runtime.env, the unused
	ones blank, and keeps the bundled Redis's URL for the way back."""
	bundled = app.backend.redis.config.get_url()      # the test Redis counts as bundled
	client = client_for(admin)
	assert client.post("/admin/server/redis/save", json={
		"host": "cache.example.org", "port": "6380"}).json["status"] == "ok"
	# unused keys blank so nothing inherited wins; leaving the bundled Redis,
	# its address is kept for the way back
	assert dotenv_values(switch_writes_only) == {
		"REDIS_HOST": "cache.example.org", "REDIS_PORT": "6380",
		"REDIS_DB": "0", "REDIS_PASSWORD": "", "REDIS_URL": "",
		"NETROLLOUT_BUNDLED_REDIS_URL": bundled}
