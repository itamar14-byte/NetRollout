"""Error handlers, triggered by genuine failures (dead Postgres port,
unroutable Redis host, corrupt ciphertext, missing CSRF token), and the
Redis-backed rollout logger against real pub/sub."""
import time

import pytest

from src.logging_utils import RolloutLogger

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

JSON = {"Content-Type": "application/json"}


@pytest.mark.parametrize("trigger", ["pg_down", "redis_timeout"])
def test_backend_outage_renders_db_error_page(client_for, trigger):
	resp = client_for().get(f"/_test/{trigger}")
	assert resp.status_code == 503
	body = resp.get_data(as_text=True)
	assert "running" in body  # health of the app's own (healthy) services


@pytest.mark.parametrize("trigger", ["pg_down", "redis_timeout"])
def test_backend_outage_json_and_sse_get_json_503(client_for, trigger):
	c = client_for()
	resp = c.get(f"/_test/{trigger}", headers=JSON, data="{}")
	assert resp.status_code == 503 and resp.json["status"] == "error"
	sse = c.get(f"/rollout/stream/_test/{trigger}")
	assert sse.status_code == 503 and sse.is_json


def test_bad_encryption_key_renders_key_error(client_for):
	c = client_for()
	page = c.get("/_test/bad_key")
	assert page.status_code == 500
	assert "Decryption failed" in page.get_data(as_text=True)
	api = c.get("/_test/bad_key", headers=JSON, data="{}")
	assert api.status_code == 500 and "Encryption key" in api.json["message"]


def test_csrf_failure_redirects_home_or_returns_json(app, client_for,
                                                     make_user):
	app.config["WTF_CSRF_ENABLED"] = True  # restored after the test
	c = client_for(make_user())
	form = c.post("/inventory/create", data={"label": "x"})
	assert form.status_code == 302 and form.headers["Location"] == "/"
	api = c.post("/properties/create", json={"name": "a", "label": "a"})
	assert api.json["message"] == "Session expired"


# ── Rollout logger over real Redis pub/sub ───────────────────────────────────

def test_logger_publishes_history_and_live_messages(app):
	client = app.backend.redis.client
	logger = RolloutLogger(webapp=True, verbose=False, job_id="job-int",
	                       redis_client=client)
	pubsub = logger.subscribe()
	pubsub.get_message(timeout=1)  # subscribe confirmation
	logger.notify("device up", "green")          # not important: not streamed
	logger.notify("rollout started", important=True)
	logger.notify("boom <script>", "red")        # errors always streamed
	history = logger.get_history()
	assert history[0] == "rollout started"
	assert history[1] == '<div class="text-danger">boom &lt;script&gt;</div>'
	live = []
	deadline = time.time() + 3
	while len(live) < 2 and time.time() < deadline:
		msg = pubsub.get_message(timeout=0.5)
		if msg and msg["type"] == "message":
			live.append(msg["data"].decode())
	assert live == history
	logger.redis_cleanup()
	assert client.exists("job:job-int:history") == 0
	pubsub.close()
