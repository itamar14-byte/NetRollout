"""Error handlers, triggered by genuine failures (dead Postgres port,
unroutable Redis host, corrupt ciphertext, missing CSRF token), and the
Redis-backed rollout logger against real pub/sub."""
import time

import pytest

from src.encryption import decrypt
from src.logging_utils import RolloutLogger

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

JSON = {"Content-Type": "application/json"}


@pytest.mark.parametrize("trigger", ["pg_down", "redis_timeout"])
def test_backend_outage_renders_db_error_page(client_for, trigger):
	"""A Postgres or Redis outage on a page gives the 503 error page, which
	shows the app's own services as running (cases: pg_down, redis_timeout)."""
	resp = client_for().get(f"/_test/{trigger}")
	assert resp.status_code == 503
	body = resp.get_data(as_text=True)
	assert "running" in body  # health of the app's own (healthy) services


@pytest.mark.parametrize("trigger", ["pg_down", "redis_timeout"])
def test_backend_outage_json_and_sse_get_json_503(client_for, trigger):
	"""A Postgres or Redis outage on a JSON request and on the live log stream
	answers 503 JSON (cases: pg_down, redis_timeout)."""
	c = client_for()
	resp = c.get(f"/_test/{trigger}", headers=JSON, data="{}")
	assert resp.status_code == 503 and resp.json["status"] == "error"
	sse = c.get(f"/rollout/stream/_test/{trigger}")
	assert sse.status_code == 503 and sse.is_json


def test_bad_encryption_key_admin_gets_the_fix(client_for, make_user):
	"""A decryption failure shows an admin a 500 page with the fix (where the
	key is read from, restore it, don't generate a new one) and the raw error,
	with no scripts."""
	page = client_for(make_user(role="admin")).get("/_test/bad_key")
	assert page.status_code == 500
	body = page.get_data(as_text=True)
	assert "restore the original key" in body
	assert "This server reads its key from" in body
	assert "NETROLLOUT_ENCRYPTION_KEY" in body
	assert "Don't generate a new key" in body
	assert "Decryption failed" in body            # raw error, admins only
	assert "<script" not in body                  # renders with no scripts


@pytest.mark.parametrize("role", [None, "operator"])
def test_bad_encryption_key_others_are_told_to_ask_an_admin(client_for,
                                                            make_user, role):
	"""Signed out or an operator, a decryption failure says to ask the
	administrator and shows none of the internals."""
	client = client_for(make_user(role=role) if role else None)
	body = client.get("/_test/bad_key").get_data(as_text=True)
	assert "Ask your NetRollout administrator" in body
	for internal in ("Decryption failed", "encryption.key",
	                 "This server reads its key from", "UPDATE users"):
		assert internal not in body


def test_bad_encryption_key_json_stays_json(client_for):
	"""A decryption failure on a JSON request answers 500 JSON naming the
	encryption key."""
	api = client_for().get("/_test/bad_key", headers=JSON, data="{}")
	assert api.status_code == 500 and "Encryption key" in api.json["message"]


def test_bad_encryption_key_at_2fa_points_admins_to_the_log(
		app, client_for, monkeypatch, capsys):
	"""A decryption failure at the 2FA step names the two-factor secret and
	the way out (an account without 2FA); the log says "Decryption failed (2fa)"
	and points to Reset 2FA."""

	def otp_verify():
		decrypt("gAAAAA-not-a-real-token")
	monkeypatch.setitem(app.view_functions, "auth.otp_verify", otp_verify)
	body = client_for().get("/otp_verify").get_data(as_text=True)
	assert "two-factor sign-in secret" in body
	assert "Sign in with the" in body and "doesn't use 2FA" in body
	log = capsys.readouterr().err
	assert "Decryption failed (2fa)" in log and "Reset 2FA" in log


def test_bad_encryption_key_after_post_retries_via_referrer(
		app, client_for, make_user, monkeypatch):
	"""A decryption failure after a POST (starting a rollout) names the
	security profile's password (escaped) and offers Retry to the referring
	page."""

	def start():
		decrypt("gAAAAA-not-a-real-token")
	monkeypatch.setitem(app.view_functions, "rollout.new_start_rollout", start)
	body = client_for(make_user(role="admin")).post(
		"/rollout/start", headers={"Referer": "http://localhost/rollout/new"}
	).get_data(as_text=True)
	assert "a security profile&#39;s password" in body  # escaped {{ what }}
	assert 'href="http://localhost/rollout/new">Retry' in body


def test_csrf_failure_redirects_home_or_returns_json(app, client_for,
                                                     make_user):
	"""With CSRF on, a form post without a token redirects to /, a JSON post
	gets "Session expired"."""
	app.config["WTF_CSRF_ENABLED"] = True  # restored after the test
	c = client_for(make_user())
	form = c.post("/inventory/create", data={"label": "x"})
	assert form.status_code == 302 and form.headers["Location"] == "/"
	api = c.post("/properties/create", json={"name": "a", "label": "a"})
	assert api.json["message"] == "Session expired"


# ── Rollout logger over real Redis pub/sub ───────────────────────────────────

def test_logger_publishes_history_and_live_messages(app):
	"""RolloutLogger over real Redis: important messages and errors (escaped,
	in red) go to the history and to pub/sub in the same order, others don't;
	redis_cleanup removes the history."""
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
