"""System Settings routes and the places that read the settings: saving
(audited, all-or-nothing), reset, the Access test, restart-pending, and
device parallelism / reachability cache / worker count wiring."""
import pytest

from src.db.settings import seed_settings
from src.db.tables import AuditLog
from src.webapp.startup import Probe

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user, session_scope):
	with session_scope() as s:       # what install() does at every startup
		seed_settings(s)
	return make_user(role="admin")


def save(client, **values):
	return client.post("/admin/settings", json={"values": values})


def audit(session_scope, action):
	with session_scope() as s:
		return [(a.object_label, a.detail) for a in
		        s.query(AuditLog).filter_by(action=action).order_by(AuditLog.timestamp)]


def test_save_writes_rows_and_audits_each_change(admin, app, client_for,
                                                 session_scope):
	resp = save(client_for(admin), job_retention_days="45",
	            device_parallelism=20, audit_retention_days=90)   # unchanged
	assert resp.status_code == 200
	assert sorted(resp.json["changed"]) == ["device_parallelism",
	                                        "job_retention_days"]
	shown = {d["key"]: d for d in resp.json["settings"]}
	assert shown["job_retention_days"]["value"] == 45
	assert app.backend.settings.get("device_parallelism") == 20
	assert sorted(audit(session_scope, "settings.update")) == [
		("device_parallelism", {"key": "device_parallelism", "old": 10, "new": 20}),
		("job_retention_days", {"key": "job_retention_days", "old": 30, "new": 45})]


def test_invalid_values_are_reported_per_field_and_save_nothing(admin, app,
                                                                client_for):
	resp = save(client_for(admin), job_retention_days=45, audit_retention_days=3,
	            public_hostname="https://nr.corp")
	assert resp.status_code == 422
	assert set(resp.json["errors"]) == {"audit_retention_days", "public_hostname"}
	assert "no https://" in resp.json["errors"]["public_hostname"]
	assert app.backend.settings.get("job_retention_days") == 30


def test_rule_violations_are_reported_and_save_nothing(admin, app, client_for):
	resp = save(client_for(admin), job_retention_days=90)   # logs stay 60
	assert resp.status_code == 422
	assert "at least as long as job records" in resp.json["errors"]["_rules"]
	assert app.backend.settings.get("job_retention_days") == 30


def test_the_server_rejects_what_the_page_would_block(admin, client_for):
	# a crafted request skipping the page's checks gets the same answers
	client = client_for(admin)
	for values in ({"device_parallelism": 0}, {"https_port": "99999"},
	               {"no_such_setting": 1}, {"public_hostname": "a b"}):
		assert save(client, **values).status_code == 422, values
	assert client.post("/admin/settings", json={"values": "x"}).status_code == 400


def test_reset(admin, app, client_for, session_scope):
	client = client_for(admin)
	save(client, job_retention_days=90, log_retention_days=120)
	refused = client.post("/admin/settings/log_retention_days/reset")
	assert refused.status_code == 422 and "_rules" in refused.json["errors"]
	resp = client.post("/admin/settings/job_retention_days/reset")
	assert resp.json["changed"] == ["job_retention_days"]
	assert app.backend.settings.get("job_retention_days") == 30
	assert audit(session_scope, "settings.reset") == [
		("job_retention_days", {"key": "job_retention_days", "old": 90, "new": 30})]
	assert client.post("/admin/settings/nope/reset").status_code == 404


def test_access_test_runs_the_proxy_check(admin, app, client_for, monkeypatch):
	calls = []

	def fake_check(url, token):
		calls.append((url, token))
		return Probe(True, ""), Probe(False, "timed out connecting to nr.corp:8443",
		                              unreachable=True)
	monkeypatch.setattr("src.webapp.blueprints.admin_settings.check_proxy",
	                    fake_check)
	client = client_for(admin)
	resp = client.post("/admin/settings/test",
	                   json={"hostname": "nr.corp", "port": 8443})
	assert resp.json["url"] == "https://nr.corp:8443"
	assert resp.json["source"] == "from System Settings"
	assert resp.json["local"] == {"ok": True, "reason": ""}
	assert resp.json["public"]["ok"] is False
	assert calls == [("https://nr.corp:8443", app.config["INSTANCE_TOKEN"])]
	bad = client.post("/admin/settings/test", json={"hostname": "x/y", "port": 443})
	assert bad.status_code == 422


def test_restart_pending_follows_the_worker_count(admin, app, client_for):
	started = app.config["SETTINGS_STARTED_WITH"]["orchestrator_workers"]
	client = client_for(admin)
	resp = save(client, orchestrator_workers=started + 2)
	assert resp.json["restart_pending"] == ["Concurrent rollout jobs"]
	resp = save(client, orchestrator_workers=started)
	assert resp.json["restart_pending"] == []


def test_orchestrator_started_with_the_setting(app):
	assert app.orchestrator.max_concurrent == \
	       app.config["SETTINGS_STARTED_WITH"]["orchestrator_workers"]


def test_rollouts_use_the_device_parallelism_setting(admin, app, make_user,
                                                     make_profile, make_device,
                                                     client_for, captured_submits):
	app.backend.settings.update({"device_parallelism": 3}, admin.id)
	user = make_user()
	dev = make_device(user, ip="10.0.0.1", profile_id=make_profile(user))
	client_for(user).post("/rollout/start", data={
		"device_ids": [str(dev)], "manual_commands": "hostname r1"})
	(call,) = captured_submits
	assert call.params.max_workers == 3


def test_reachability_cache_uses_the_setting(admin, app, monkeypatch):
	app.backend.settings.update({"reachability_cache_seconds": 25}, admin.id)
	checker = app.web.reachability
	monkeypatch.setattr(checker, "_probe", lambda ip, port: True)  # no network
	checker.check([("10.9.9.9", 22)], refresh=True)
	ttl = app.backend.redis.client.ttl("reach:10.9.9.9:22")
	assert 20 <= ttl <= 25


def test_non_admins_cant_change_settings(client_for, make_user, app):
	client = client_for(make_user())
	resp = client.post("/admin/settings", json={"values": {"device_parallelism": 1}})
	assert resp.status_code in (302, 403)
	assert app.backend.settings.get("device_parallelism") == 10


def test_page_renders_every_setting_with_the_shared_rules(admin, client_for):
	html = client_for(admin).get("/admin/settings").get_data(as_text=True)
	for key in ("job_retention_days", "orchestrator_workers",
	            "public_hostname", "https_port"):
		assert f'id="set-{key}"' in html, key
	assert 'min="7"' in html and 'max="65535"' in html     # ranges → inputs
	assert "at least as long as job records" in html        # rules → the page
	assert "Internal app port" in html and 'id="testBtn"' in html


def test_the_sessions_card_offers_the_idle_timeout(admin, client_for):
	html = client_for(admin).get("/admin/settings").get_data(as_text=True)
	assert ">Sessions<" in html and 'id="set-session_idle_minutes"' in html
	assert "Sign out after inactivity" in html
	assert 'min="5"' in html and 'max="480"' in html


def test_restart_dot_shows_on_admin_pages_while_pending(admin, app, client_for):
	client = client_for(admin)
	started = app.config["SETTINGS_STARTED_WITH"]["orchestrator_workers"]
	html = client.get("/admin/users").get_data(as_text=True)
	assert 'title="Restart server"' in html
	assert 'id="restartPendingDot" style="display:none' in html
	save(client, orchestrator_workers=started + 1)
	html = client.get("/admin/users").get_data(as_text=True)
	assert 'title="Restart required — Concurrent rollout jobs changed"' in html
	assert 'id="restartPendingDot" style="display:inline-block' in html
