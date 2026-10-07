"""System Settings routes and the places that read the settings: saving
(audited, all-or-nothing), reset, the Access test, restart-pending, and
device parallelism / reachability cache / worker count wiring."""
import json as _json
import time as _time

import pytest

from src import runtime as _runtime
from src.access import certs as _certs, site_env as _site, nginx as _pc, port as _pa
from src.db.settings import seed_settings, SettingsError
from src.db.tables import AuditLog
from src.webapp.blueprints import admin_settings
from src.webapp.startup import Probe
from tests.unit.access.test_certs import key_pem, make_cert, pem

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user, session_scope):
	"""An admin, with every setting seeded first."""
	with session_scope() as s:       # what install() does at every startup
		seed_settings(s)
	return make_user(role="admin")


def save(client, **values):
	return client.post("/admin/settings", json={"values": values})


def audit(session_scope, action):
	"""(object_label, detail) of every audit row with `action`, oldest first."""
	with session_scope() as s:
		return [(a.object_label, a.detail) for a in
		        s.query(AuditLog).filter_by(action=action).order_by(AuditLog.timestamp)]


def test_save_writes_rows_and_audits_each_change(admin, app, client_for,
                                                 session_scope):
	"""Saving writes only the changed settings (a string value parsed) and audits each
	change with its old and new value; an unchanged value is neither saved nor audited."""
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
	"""Invalid values are answered 422 with an error per field (a hostname with
	https:// refused) and nothing is saved, not even the valid value."""
	resp = save(client_for(admin), job_retention_days=45, audit_retention_days=3,
	            public_hostname="https://nr.corp")
	assert resp.status_code == 422
	assert set(resp.json["errors"]) == {"audit_retention_days", "public_hostname"}
	assert "no https://" in resp.json["errors"]["public_hostname"]
	assert app.backend.settings.get("job_retention_days") == 30


def test_rule_violations_are_reported_and_save_nothing(admin, app, client_for):
	"""A save that breaks a rule (job records longer than logs) is 422 with the rule's
	message under `_rules` and saves nothing."""
	resp = save(client_for(admin), job_retention_days=90)   # logs stay 60
	assert resp.status_code == 422
	assert "at least as long as job records" in resp.json["errors"]["_rules"]
	assert app.backend.settings.get("job_retention_days") == 30


def test_the_server_rejects_what_the_page_would_block(admin, client_for):
	"""A crafted request skipping the page's checks gets the same answers: out-of-range,
	unknown or illegal values are 422, a malformed body 400."""
	client = client_for(admin)
	for values in ({"device_parallelism": 0}, {"https_port": "99999"},
	               {"no_such_setting": 1}, {"public_hostname": "a b"}):
		assert save(client, **values).status_code == 422, values
	assert client.post("/admin/settings", json={"values": "x"}).status_code == 400


def test_reset(admin, app, client_for, session_scope):
	"""Reset puts a setting back to its default (saved and audited); a reset that breaks
	a rule is 422 and an unknown key is 404."""
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
	"""The Access test checks the given hostname and port with this instance's token and
	reports the local and public results; an illegal hostname is 422."""
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
	"""Changing the concurrent rollout jobs marks a restart pending; setting it back to
	the value the app started with clears it."""
	started = app.config["SETTINGS_STARTED_WITH"]["orchestrator_workers"]
	client = client_for(admin)
	resp = save(client, orchestrator_workers=started + 2)
	assert resp.json["restart_pending"] == ["Concurrent rollout jobs"]
	resp = save(client, orchestrator_workers=started)
	assert resp.json["restart_pending"] == []


def test_orchestrator_started_with_the_setting(app):
	"""The orchestrator runs as many jobs at once as the setting said at start."""
	assert app.orchestrator.max_concurrent == \
	       app.config["SETTINGS_STARTED_WITH"]["orchestrator_workers"]


def test_rollouts_use_the_device_parallelism_setting(admin, app, make_user,
                                                     make_profile, make_device,
                                                     client_for, captured_submits):
	"""A rollout is submitted with the devices-per-job setting as its max workers."""
	app.backend.settings.update({"device_parallelism": 3}, admin.id)
	user = make_user()
	dev = make_device(user, ip="10.0.0.1", profile_id=make_profile(user))
	client_for(user).post("/rollout/start", data={
		"device_ids": [str(dev)], "manual_commands": "hostname r1"})
	(call,) = captured_submits
	assert call.params.max_workers == 3


def test_reachability_cache_uses_the_setting(admin, app, monkeypatch):
	"""A reachability result is cached in Redis for the reachability cache setting's
	seconds (TTL 20-25 for a setting of 25)."""
	app.backend.settings.update({"reachability_cache_seconds": 25}, admin.id)
	checker = app.web.reachability
	monkeypatch.setattr(checker, "_probe", lambda ip, port: True)  # no network
	checker.check([("10.9.9.9", 22)], refresh=True)
	ttl = app.backend.redis.client.ttl("reach:10.9.9.9:22")
	assert 20 <= ttl <= 25


def test_non_admins_cant_change_settings(client_for, make_user, app):
	"""A non-admin's save is refused (redirect or 403) and the setting keeps its
	default."""
	client = client_for(make_user())
	resp = client.post("/admin/settings", json={"values": {"device_parallelism": 1}})
	assert resp.status_code in (302, 403)
	assert app.backend.settings.get("device_parallelism") == 10


def test_page_renders_every_setting_with_the_shared_rules(admin, client_for):
	"""The settings page has an input per setting, the ranges as min/max, the rules'
	messages, the internal app port and the Test button."""
	html = client_for(admin).get("/admin/settings").get_data(as_text=True)
	for key in ("job_retention_days", "orchestrator_workers",
	            "public_hostname", "https_port"):
		assert f'id="set-{key}"' in html, key
	assert 'min="7"' in html and 'max="65535"' in html     # ranges → inputs
	assert "at least as long as job records" in html        # rules → the page
	assert "Internal app port" in html and 'id="testBtn"' in html


def test_the_sessions_card_offers_the_idle_timeout(admin, client_for):
	"""The Sessions card offers the idle timeout input with its 5-480 range."""
	html = client_for(admin).get("/admin/settings").get_data(as_text=True)
	assert ">Sessions<" in html and 'id="set-session_idle_minutes"' in html
	assert "Sign out after inactivity" in html
	assert 'min="5"' in html and 'max="480"' in html


def test_restart_dot_shows_on_admin_pages_while_pending(admin, app, client_for):
	"""Admin pages hide the restart-pending dot until the worker count changes, then show
	it with a tooltip naming the changed setting."""
	client = client_for(admin)
	started = app.config["SETTINGS_STARTED_WITH"]["orchestrator_workers"]
	html = client.get("/admin/users").get_data(as_text=True)
	assert 'title="Restart server"' in html
	assert 'id="restartPendingDot" style="display:none' in html
	save(client, orchestrator_workers=started + 1)
	html = client.get("/admin/users").get_data(as_text=True)
	assert 'title="Restart required — Concurrent rollout jobs changed"' in html
	assert 'id="restartPendingDot" style="display:inline-block' in html


# ── The hostname: applied by nginx at once, all or nothing ──────────────────




@pytest.fixture
def proxy(app, monkeypatch):
	"""Clean nginx folder + certs folder; hostname back to empty afterwards.
	`managed()` makes it look like NetRollout's nginx reports there;
	`verdict(...)` is what the watcher answers."""
	site_dir, cert_dir = _site.folder(), _runtime.certs_dir()
	# what was there (e.g. the site.env the app wrote at startup) comes back
	found = {p: p.read_bytes() for d in (site_dir, cert_dir) if d.exists()
	         for p in d.iterdir() if p.is_file()}

	def wipe():
		for d in (site_dir, cert_dir):
			if d.exists():
				for p in d.iterdir():
					p.unlink()
	wipe()
	answer = {"status": None}
	monkeypatch.setattr(_pc, "wait_for_status",
	                    lambda after, hostname=None, **kw: answer["status"])

	class Proxy:
		site = site_dir / _pc.SITE_FILE
		cert = cert_dir / _certs.CERT_FILE

		@staticmethod
		def managed():
			site_dir.mkdir(parents=True, exist_ok=True)
			(site_dir / _pc.STATUS_FILE).write_text(_json.dumps(
				{"state": "applied", "message": "hostname=(none) https_port=443",
				 "time": "2026-01-01T00:00:00Z"}))

		@staticmethod
		def verdict(state, message=""):
			answer["status"] = {"state": state, "message": message} if state else None

		@staticmethod
		def files():
			return {p.name: p.read_bytes() for d in (site_dir, cert_dir)
			        if d.exists() for p in d.iterdir() if p.name != _pc.STATUS_FILE}
	yield Proxy
	app.backend.settings.update({"public_hostname": ""}, None)
	wipe()
	for path, data in found.items():
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(data)


def hostname(app):
	"""The saved public hostname."""
	return app.backend.settings.get("public_hostname")


def test_a_new_hostname_reaches_nginx(admin, app, client_for, proxy):
	"""A saved hostname is written into site.env; with no nginx reporting it is
	`not_managed`."""
	resp = save(client_for(admin), public_hostname="nr01.corp.local")
	assert resp.status_code == 200
	assert resp.json["proxy"] == {"state": "not_managed"}       # no nginx here
	assert "NETROLLOUT_HOSTNAME=nr01.corp.local\n" in proxy.site.read_text()


def test_nginx_applying_it_is_reported(admin, app, client_for, proxy):
	"""nginx's verdict comes back with the save: `applied`, or `no_answer` when nginx
	stays silent (the save still succeeds)."""
	proxy.managed()
	proxy.verdict("applied", "hostname=nr01.corp.local https_port=443 app=app:8080")
	resp = save(client_for(admin), public_hostname="nr01.corp.local")
	assert resp.json["proxy"]["state"] == "applied"
	proxy.verdict(None)                                         # silence
	resp = save(client_for(admin), public_hostname="nr02.corp.local")
	assert resp.status_code == 200 and resp.json["proxy"] == {"state": "no_answer"}


def test_a_self_signed_certificate_is_reissued_for_the_new_name(
		admin, app, client_for, proxy):
	"""A new hostname reissues the self-signed certificate for it, keeping its IPs and the
	previous name, whose deadline is the transition period from now."""
	_certs.selfsigned("old.lab", ["10.0.0.5"], _runtime.certs_dir())
	assert save(client_for(admin), public_hostname="new.lab").status_code == 200
	dns, ips = _certs.names_in(proxy.cert.read_bytes())
	# the previous name stays for the transition period (approved 2026-10-04)
	assert dns == ["new.lab", "old.lab"] and [str(i) for i in ips] == ["10.0.0.5"]
	assert _certs.is_selfsigned(_runtime.certs_dir())
	until = _json.loads((_runtime.certs_dir() / _pc.OLD_NAMES_FILE).read_text())["old.lab"]
	assert abs(until - (_time.time() + _pc.NAME_TRANSITION_DAYS * 86400)) < 60


def _org_cert(names):
	"""An organisation's certificate (no self-signed marker) for `names`."""
	cert, key = make_cert(names=names)
	d = _runtime.certs_dir()
	d.mkdir(parents=True, exist_ok=True)
	(d / _certs.CERT_FILE).write_bytes(pem(cert))
	(d / _certs.KEY_FILE).write_bytes(key_pem(key))


def test_an_organisation_certificate_that_covers_it_stays(admin, app,
                                                          client_for, proxy):
	"""An organisation's certificate that covers the new hostname is left untouched."""
	_org_cert(("*.corp.local",))
	before = proxy.cert.read_bytes()
	assert save(client_for(admin), public_hostname="nr01.corp.local").status_code == 200
	assert proxy.cert.read_bytes() == before


def test_one_that_does_not_cover_it_refuses_and_changes_nothing(
		admin, app, client_for, proxy):
	"""An organisation's certificate that doesn't cover the new hostname refuses the save
	(422, naming both) and changes no setting or file."""
	_org_cert(("nr01.corp.local",))
	before = proxy.files()
	resp = save(client_for(admin), public_hostname="other.example")
	assert resp.status_code == 422
	assert "covers nr01.corp.local — not other.example" in \
	       resp.json["errors"]["public_hostname"]
	assert hostname(app) == "" and proxy.files() == before


def test_a_file_that_cannot_be_written_changes_nothing(admin, app, client_for,
                                                       proxy, monkeypatch):
	"""When site.env can't be written the save is 422 with the reason, and the already
	reissued certificate is put back: no setting or file changed."""
	_certs.selfsigned("old.lab", ["10.0.0.5"], _runtime.certs_dir())
	before = proxy.files()

	def no_permission(hostname):
		raise PermissionError(13, "Permission denied", str(proxy.site))
	monkeypatch.setattr(_pc, "write_site", no_permission)
	resp = save(client_for(admin), public_hostname="new.lab")
	assert resp.status_code == 422
	message = resp.json["errors"]["public_hostname"]
	assert "couldn't write" in message and "Permission denied" in message
	# the certificate was already reissued — and put back
	assert hostname(app) == "" and proxy.files() == before


def test_nginx_rejecting_it_puts_everything_back(admin, app, client_for, proxy):
	"""When nginx rejects the new hostname the save is 422 with nginx's message and the
	setting, site.env and certificate are the old ones again."""
	_certs.selfsigned("old.lab", ["10.0.0.5"], _runtime.certs_dir())
	app.backend.settings.update({"public_hostname": "old.lab"}, None)
	_pc.write_site("old.lab")
	before = proxy.files()
	proxy.managed()
	proxy.verdict("rejected", "nginx: [emerg] something")
	resp = save(client_for(admin), public_hostname="new.lab")
	assert resp.status_code == 422
	assert "nginx rejected the new hostname: nginx: [emerg] something" in \
	       resp.json["errors"]["public_hostname"]
	assert hostname(app) == "old.lab" and proxy.files() == before


def test_nginx_rejecting_the_hostname_undoes_the_whole_save(admin, app, client_for,
                                                            proxy):
	"""A hostname nginx rejects undoes everything saved with it, as the message
	says ("nothing was changed"): the other settings keep their old values and
	no port request is left for the helper."""
	proxy.managed()
	proxy.verdict("rejected", "nginx: [emerg] something")
	resp = save(client_for(admin), public_hostname="new.lab",
	            job_retention_days=45, https_port=9443)
	assert resp.status_code == 422
	assert "nothing was changed" in resp.json["errors"]["public_hostname"]
	settings = app.backend.settings
	assert (settings.get("public_hostname"), settings.get("job_retention_days"),
	        settings.get("https_port")) == ("", 30, 443)
	assert _site.PORT_REQUEST not in _site.read()


def test_an_illegal_hostname_never_reaches_nginx(admin, app, client_for, proxy):
	"""An illegal hostname is 422 and no site.env is written."""
	resp = save(client_for(admin), public_hostname="nr01;evil")
	assert resp.status_code == 422 and not proxy.site.exists()


def test_resetting_the_hostname_goes_through_nginx_too(admin, app, client_for,
                                                       proxy):
	"""Resetting the hostname empties it in the setting and in site.env."""
	client = client_for(admin)
	save(client, public_hostname="nr01.corp.local")
	resp = client.post("/admin/settings/public_hostname/reset")
	assert resp.status_code == 200 and hostname(app) == ""
	assert "NETROLLOUT_HOSTNAME=\n" in proxy.site.read_text()


def test_the_audit_says_what_nginx_did(admin, app, client_for, proxy,
                                       session_scope):
	"""The hostname's audit entry records nginx's outcome (`not_managed` here)."""
	save(client_for(admin), public_hostname="nr01.corp.local")
	(detail,) = [d for label, d in audit(session_scope, "settings.update")
	             if label == "public_hostname"]
	assert detail["nginx"] == "not_managed"


# ── The HTTPS port: requested from the port helper, all or nothing ──────────




@pytest.fixture
def port_files(app):
	"""The contract files — site.env (the request, the confirmation) and the
	helper's status — what was there comes back; the setting back to 443
	afterwards. `helper(...)` writes the helper's status the way the contract
	says."""
	status_file = _runtime.config_dir() / _pa.STATUS_FILE
	files = [status_file, _site.path()]
	found = {f: f.read_bytes() for f in files if f.is_file()}
	for f in files:
		f.unlink(missing_ok=True)

	class Files:
		@staticmethod
		def requested():
			return _site.read().get(_site.PORT_REQUEST)

		@staticmethod
		def confirmed():
			return _site.read().get(_site.PORT_CONFIRMED)

		@staticmethod
		def request_keys():
			return {k: v for k, v in _site.read().items()
			        if k.startswith("NETROLLOUT_PORT_")}

		@staticmethod
		def helper(**status):
			status_file.write_text(_json.dumps(status))
	yield Files
	app.backend.settings.update({"https_port": 443}, None)
	for f in files:
		f.unlink(missing_ok=True)
	for f, data in found.items():
		f.parent.mkdir(parents=True, exist_ok=True)
		f.write_bytes(data)


def test_a_new_port_is_requested_and_saved(admin, app, client_for, port_files,
                                            session_scope):
	"""A new HTTPS port is saved and requested in site.env; with no helper the page state
	is `manual` (still serving 443) and the audit entry says so."""
	resp = save(client_for(admin), https_port=8443)
	assert resp.status_code == 200
	assert app.backend.settings.get("https_port") == 8443
	assert port_files.requested() == "8443"
	# no helper here: the page says to run `netrollout apply`
	assert resp.json["port"] == {"saved": 8443, "serving": 443, "state": "manual"}
	(detail,) = [d for label, d in audit(session_scope, "settings.update")
	             if label == "https_port"]
	assert detail["port_apply"] == "manual"


def test_an_invalid_port_requests_nothing(admin, app, client_for, port_files):
	"""An out-of-range port is 422 and writes no port request."""
	resp = save(client_for(admin), https_port=70000)
	assert resp.status_code == 422 and port_files.requested() is None


def test_a_request_that_cannot_be_written_saves_nothing(
		admin, app, client_for, port_files, proxy, monkeypatch):
	"""When the port request can't be written the save is 422 with the reason; neither
	the port nor a hostname in the same save is kept (certificate and site.env back)."""
	_certs.selfsigned("old.lab", ["10.0.0.5"], _runtime.certs_dir())
	before = proxy.files()

	real_update = _site.update

	def no_permission(values):                  # only the port request fails
		if _site.PORT_REQUEST in values:
			raise PermissionError(13, "Permission denied", str(_site.path()))
		return real_update(values)
	monkeypatch.setattr(_site, "update", no_permission)
	# a hostname in the same save: its certificate and site.env come back too
	resp = save(client_for(admin), https_port=8443, public_hostname="new.lab")
	assert resp.status_code == 422
	message = resp.json["errors"]["https_port"]
	assert "couldn't write" in message and "Permission denied" in message
	assert app.backend.settings.get("https_port") == 443
	assert hostname(app) == "" and proxy.files() == before


def test_a_save_that_fails_late_takes_the_request_back(
		admin, app, client_for, port_files, monkeypatch):
	"""When saving the setting fails after the port request was written, the previous
	request is put back."""
	_pa.request_port(9443)
	before = port_files.request_keys()

	def changed_meanwhile(values, user_id):
		raise SettingsError({"https_port": "changed meanwhile"})
	real = app.backend.settings.update
	monkeypatch.setattr(app.backend.settings, "update", changed_meanwhile)
	assert save(client_for(admin), https_port=8443).status_code == 422
	monkeypatch.setattr(app.backend.settings, "update", real)   # port_files cleans up with it
	assert port_files.request_keys() == before


def test_resetting_the_port_is_requested_too(admin, app, client_for, port_files):
	"""Resetting the port saves 443 and requests it from the helper."""
	client = client_for(admin)
	save(client, https_port=8443)
	resp = client.post("/admin/settings/https_port/reset")
	assert resp.status_code == 200 and app.backend.settings.get("https_port") == 443
	assert port_files.requested() == "443"


def test_the_page_follows_a_trial(admin, app, client_for, port_files):
	"""While the helper tries the new port, the page's port status reports `trying` with
	the trial port and the request id."""
	client = client_for(admin)
	save(client, https_port=8443)
	rid = _pa.read_request()["id"]
	port_files.helper(state="trying", port=443, trying=8443, id=rid,
	                  deadline=_time.time() + 120)
	port = client.get("/admin/settings/port?_bg=1").json["port"]
	assert port["state"] == "trying" and port["trying"] == 8443 and port["id"] == rid


def test_confirming_from_the_new_port(admin, app, client_for, port_files, proxy):
	"""A trial is confirmed only through the new port (the old one is 409); confirming
	records it in site.env, sets the state to `confirming` and the port in use to it."""
	client = client_for(admin)
	save(client, https_port=8443)
	rid = _pa.read_request()["id"]
	port_files.helper(state="trying", port=443, trying=8443, id=rid,
	                  deadline=_time.time() + 120)
	# through the old port: refused
	old = client.post("/admin/settings/port/confirm", json={"id": rid},
	                  base_url="https://localhost")
	assert old.status_code == 409 and port_files.confirmed() is None
	new = client.post("/admin/settings/port/confirm", json={"id": rid},
	                  base_url="https://localhost:8443")
	assert new.status_code == 200 and new.json["port"]["state"] == "confirming"
	assert port_files.confirmed() == rid
	# redirects follow the confirmed port at once
	assert "NETROLLOUT_HTTPS_PORT=8443\n" in proxy.site.read_text()


def test_only_admins_confirm(make_user, client_for, port_files):
	"""An operator can't confirm a port trial (redirect or 403) and nothing is recorded."""
	client = client_for(make_user(role="operator"))
	resp = client.post("/admin/settings/port/confirm", json={"id": "x"},
	                   base_url="https://localhost:8443")
	assert resp.status_code in (302, 403) and port_files.confirmed() is None


def test_try_again_is_a_new_request(admin, app, client_for, port_files):
	"""After a rollback, Try again requests the same port under a new request id."""
	client = client_for(admin)
	save(client, https_port=8443)
	rid = _pa.read_request()["id"]
	port_files.helper(state="rolled_back", port=443, id=rid,
	                  message="not confirmed within 120 s")
	assert client.get("/admin/settings/port").json["port"]["state"] == "rolled_back"
	resp = client.post("/admin/settings/port/retry")
	assert resp.status_code == 200 and _pa.read_request()["id"] != rid
	assert _pa.read_request()["port"] == 8443


def test_old_names_leave_after_the_transition(admin, app, client_for, proxy):
	"""Previous hostnames stay in the self-signed certificate until their transition ends,
	then are dropped: the certificate keeps the current name and its IPs, and the
	old-names file is removed."""
	_certs.selfsigned("a.lab", ["10.0.0.5"], _runtime.certs_dir())
	client = client_for(admin)
	save(client, public_hostname="b.lab")
	save(client, public_hostname="c.lab")          # a.lab and b.lab both kept
	assert _certs.names_in(proxy.cert.read_bytes())[0] == ["c.lab", "a.lab", "b.lab"]
	assert _pc.drop_expired_names() == []          # nothing due yet
	later = _time.time() + _pc.NAME_TRANSITION_DAYS * 86400 + 60
	assert sorted(_pc.drop_expired_names(now=later)) == ["a.lab", "b.lab"]
	dns, ips = _certs.names_in(proxy.cert.read_bytes())
	assert dns == ["c.lab"] and [str(i) for i in ips] == ["10.0.0.5"]
	assert _certs.is_selfsigned(_runtime.certs_dir())
	assert not (_runtime.certs_dir() / _pc.OLD_NAMES_FILE).exists()


def test_going_back_to_an_old_name_ends_its_transition(admin, app, client_for,
                                                       proxy):
	"""Going back to a name in transition makes it the hostname again: it leaves the
	old-names file, and the name left behind enters it."""
	_certs.selfsigned("a.lab", [], _runtime.certs_dir())
	client = client_for(admin)
	save(client, public_hostname="b.lab")
	save(client, public_hostname="a.lab")
	assert _certs.names_in(proxy.cert.read_bytes())[0] == ["a.lab", "b.lab"]
	stored = _json.loads((_runtime.certs_dir() / _pc.OLD_NAMES_FILE).read_text())
	assert list(stored) == ["b.lab"]               # a.lab is the hostname again


def test_an_organisation_certificate_is_never_reissued(admin, app, proxy):
	"""Dropping expired old names never touches an organisation's certificate."""
	_org_cert(("nr01.corp.local", "old.corp.local"))
	(_runtime.certs_dir() / _pc.OLD_NAMES_FILE).write_text('{"old.corp.local": 1}')
	before = proxy.cert.read_bytes()
	assert _pc.drop_expired_names() == [] and proxy.cert.read_bytes() == before


def test_a_refused_save_keeps_the_old_names_file(admin, app, client_for, proxy,
                                                 monkeypatch):
	"""A hostname save refused because site.env can't be written leaves every file,
	the old-names file included, as it was."""
	_certs.selfsigned("a.lab", [], _runtime.certs_dir())
	client = client_for(admin)
	save(client, public_hostname="b.lab")
	before = proxy.files()

	def no_permission(hostname):
		raise PermissionError(13, "Permission denied", str(proxy.site))
	monkeypatch.setattr(_pc, "write_site", no_permission)
	assert save(client, public_hostname="c.lab").status_code == 422
	assert proxy.files() == before


def test_a_later_change_does_not_extend_an_old_names_deadline(
		admin, app, client_for, proxy):
	"""Another hostname change keeps an old name's existing deadline instead of
	extending it."""
	_certs.selfsigned("a.lab", [], _runtime.certs_dir())
	client = client_for(admin)
	save(client, public_hostname="b.lab")
	names = _runtime.certs_dir() / _pc.OLD_NAMES_FILE
	soon = _time.time() + 86400                       # a.lab: one day left
	names.write_text(_json.dumps({"a.lab": soon}))
	save(client, public_hostname="c.lab")
	assert _json.loads(names.read_text())["a.lab"] == soon


# ── The Access status: nginx's last verdict and the certificate, any time ───

def overview(app):
	"""The Access status for the saved hostname."""
	return _pc.overview(app.backend.settings.get("public_hostname"))


def test_status_with_nothing_there(admin, app, proxy):
	"""With no nginx status and no certificate, the Access status has neither."""
	assert overview(app) == {"nginx": None, "certificate": None}


def test_status_shows_nginxs_verdict(admin, app, proxy):
	"""The Access status shows nginx's last verdict: applied, then rejected with its
	message and time."""
	proxy.managed()
	assert overview(app)["nginx"]["state"] == "applied"
	(_site.folder() / _pc.STATUS_FILE).write_text(_json.dumps(
		{"state": "rejected", "message": "nginx: [emerg] bad", "time": "2026-10-04T10:00:00Z"}))
	assert overview(app)["nginx"] == {"state": "rejected", "message": "nginx: [emerg] bad",
	                                  "time": "2026-10-04T10:00:00Z"}


def test_status_shows_the_certificate_and_its_old_names(admin, app, client_for,
                                                        proxy):
	"""The Access status shows the self-signed certificate's names, no problems or
	warnings, and the old name with its future deadline."""
	_certs.selfsigned("a.lab", ["10.0.0.5"], _runtime.certs_dir())
	save(client_for(admin), public_hostname="b.lab")
	cert = overview(app)["certificate"]
	assert cert["selfsigned"] and cert["names"] == ["b.lab", "a.lab", "10.0.0.5"]
	assert cert["problems"] == [] and cert["warnings"] == []
	(old,) = cert["old_names"]
	assert old["name"] == "a.lab" and old["until"] > _time.time()


def test_status_flags_a_certificate_that_does_not_cover_the_hostname(
		admin, app, proxy):
	"""An organisation's certificate that doesn't cover the saved hostname is flagged
	as a problem."""
	_org_cert(("nr01.corp.local",))
	app.backend.settings.update({"public_hostname": "other.corp.local"}, None)
	cert = overview(app)["certificate"]
	assert not cert["selfsigned"]
	assert any("doesn't cover other.corp.local" in p for p in cert["problems"])


def test_status_flags_a_key_that_does_not_match(admin, app, proxy):
	"""A private key that doesn't belong to the certificate is flagged as a problem."""
	_certs.selfsigned("a.lab", [], _runtime.certs_dir())
	key = (_runtime.certs_dir() / _certs.KEY_FILE).read_bytes()
	_certs.selfsigned("a.lab", [], _runtime.certs_dir())       # a new key
	(_runtime.certs_dir() / _certs.KEY_FILE).write_bytes(key)    # the old one back
	assert any("private key doesn't belong" in p
	           for p in overview(app)["certificate"]["problems"])


def test_status_warns_before_expiry(admin, app, proxy):
	"""A certificate expiring in 5 days gets an expiry warning."""
	_certs.selfsigned("a.lab", [], _runtime.certs_dir(), days=5)
	assert any("expires on" in w for w in overview(app)["certificate"]["warnings"])


def test_the_page_and_every_save_carry_the_status(admin, app, client_for, proxy):
	"""The settings page shows the Access status and a save's answer carries it."""
	_certs.selfsigned("a.lab", [], _runtime.certs_dir())
	client = client_for(admin)
	assert b'id="accessStatus"' in client.get("/admin/settings").data
	resp = save(client, device_parallelism=11)
	assert resp.json["access"]["certificate"]["names"] == ["a.lab"]
	save(client, device_parallelism=10)


def test_in_docker_the_test_button_shows_what_nginx_reported(
		admin, app, client_for, proxy, monkeypatch):
	"""In a container the Test button answers with what nginx last reported instead of
	probing."""
	monkeypatch.setattr(admin_settings, "in_container", lambda: True)
	proxy.managed()
	resp = client_for(admin).post("/admin/settings/test",
	                              json={"hostname": "", "port": 443})
	assert resp.status_code == 200 and resp.json["container"]
	assert resp.json["access"]["nginx"]["state"] == "applied"
