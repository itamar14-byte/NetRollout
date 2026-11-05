"""Discover devices' routes (src/webapp/blueprints/discovery.py) and the
server's one scan (app.discovery), with a fake port check and sign-in: who
may use them, check's answer, start's refusals, status since N, Continue and
Cancel by another admin, creating devices through Inventory's rules (once),
the audit rows, and the Restart dialog naming a running scan."""
import datetime
import threading
import time

import netmiko
import pytest

from src.db.tables import AuditLog, DeviceCredential, Inventory
from src.discovery import scan as scan_module
from src.rollout.detect import Detection
from src.webapp.blueprints import discovery as discovery_routes

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

WAIT = 10
XE = Detection(platform="cisco_xe", detected="cisco_xe", hostname="core-sw1", evidence="Cisco IOS XE Software")
AUTH = netmiko.exceptions.NetmikoAuthenticationException("Authentication failed.")


@pytest.fixture
def devices(app, monkeypatch):
	"""The scan's network: every port open with a banner; each address's
	sign-in answer (address -> Detection or exception; XE when not set),
	optionally held by `gate`; the devices signed in to."""
	answers, calls, gate = {}, [], threading.Event()
	gate.set()

	def detect(device):
		calls.append(device)
		assert gate.wait(WAIT)
		answer = answers.get(device.ip, XE)
		if isinstance(answer, Exception):
			raise answer
		return answer

	monkeypatch.setattr(app.discovery, "knock", lambda address, port: "SSH-2.0-Cisco-1.25")
	monkeypatch.setattr(app.discovery, "detect", detect)
	monkeypatch.setattr(app.discovery, "_current", None)
	monkeypatch.setattr(scan_module, "SIGN_IN_GAP", 0.0)
	monkeypatch.setattr(discovery_routes, "sending_from", lambda address, port: "192.0.2.10")
	yield type("Devices", (), {"answers": answers, "calls": calls, "gate": gate})
	gate.set()
	if found := app.discovery.current():
		found.scan.cancel("test")
		assert found.scan.wait(WAIT)


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def start(client, profile_id, addresses="10.20.0.1-3", **extra):
	return client.post("/inventory/discover/start",
	                   json={"addresses": addresses, "port": "22", "profile_id": str(profile_id),
	                         "confirmed": True, **extra})


def ended(app):
	found = app.discovery.current()
	assert found.scan.wait(WAIT)
	return found


def audit_rows(session_scope, action):
	with session_scope() as s:
		rows = s.query(AuditLog).filter_by(action=action).order_by(AuditLog.timestamp).all()
		s.expunge_all()
		return rows


# ── who ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path", [
	("get", "/inventory/discover"), ("post", "/inventory/discover/check"), ("post", "/inventory/discover/start"),
	("get", "/inventory/discover/status"), ("post", "/inventory/discover/continue"),
	("post", "/inventory/discover/cancel"), ("post", "/inventory/discover/create"),
])
def test_operators_are_refused(client_for, make_user, devices, method, path):
	"""Every route is admins only: an operator's script gets 403."""
	resp = getattr(client_for(make_user(), xhr=True), method)(path, json={"addresses": "10.0.0.1", "cards": []})
	assert resp.status_code == 403


def test_the_page_opens_for_an_admin(client_for, admin, devices):
	assert client_for(admin).get("/inventory/discover").status_code == 200


# ── check ────────────────────────────────────────────────────────────────────

def test_check_says_what_a_start_would_send_and_starts_nothing(app, client_for, admin, make_profile, devices):
	"""The address count (overlaps once), the entries and notes, the port,
	the profile's username and the address it's sent from; no scan."""
	pid = make_profile(admin, username="netops-svc")
	resp = client_for(admin).post("/inventory/discover/check",
	                              json={"addresses": "10.20.0.5/24, 10.20.0.10-20", "port": "2222",
	                                    "profile_id": str(pid)})
	assert resp.status_code == 200
	assert resp.json == {"status": "ok", "addresses": 254, "entries": ["10.20.0.5/24", "10.20.0.10-20"],
	                     "notes": ["10.20.0.5/24 means 10.20.0.0/24", "overlapping entries counted once"],
	                     "port": 2222, "username": "netops-svc", "sources": ["192.0.2.10"], "refusal": None}
	assert app.discovery.current() is None
	assert devices.calls == []


def test_check_in_a_container_names_the_hosts_addresses(client_for, admin, make_profile, devices, monkeypatch):
	"""In Docker the container's own address isn't what the network sees:
	the installer's record of the host's addresses is named."""
	monkeypatch.setenv("NETROLLOUT_DEPLOYMENT", "docker")
	monkeypatch.setenv("NETROLLOUT_SERVER_IPS", "10.1.5.20, 192.168.0.4")
	resp = client_for(admin).post("/inventory/discover/check",
	                              json={"addresses": "10.20.0.1", "profile_id": str(make_profile(admin))})
	assert resp.json["sources"] == ["10.1.5.20", "192.168.0.4"]


@pytest.mark.parametrize("body, code, message", [
	({"addresses": "core1"}, 400, '"core1" isn\'t an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)'),
	({"addresses": "10.0.0.0/16"}, 400, "65,534 addresses - one discovery takes at most 4,096. "
	                                    "Split it, or narrow the subnet."),
	({"addresses": "10.0.0.1", "port": "70000"}, 400, "Port must be between 1 and 65535"),
	({"addresses": "10.0.0.1", "profile_id": "nope"}, 400, "Choose one of your security profiles"),
])
def test_check_refuses_what_start_would(client_for, admin, make_profile, devices, body, code, message):
	"""The parser's message, the port, a profile that isn't an id."""
	make_profile(admin)
	resp = client_for(admin).post("/inventory/discover/check", json=body)
	assert (resp.status_code, resp.json["message"]) == (code, message)


def test_another_admins_profile_is_refused(client_for, admin, make_user, make_profile, devices):
	"""Only your own profile signs in: another admin's is "not found",
	for check and for start."""
	theirs = make_profile(make_user(role="admin"))
	client = client_for(admin)
	resp = client.post("/inventory/discover/check", json={"addresses": "10.0.0.1", "profile_id": str(theirs)})
	assert (resp.status_code, resp.json["message"]) == (404, "Choose one of your security profiles")
	assert start(client, theirs).status_code == 404
	assert devices.calls == []


# ── start ────────────────────────────────────────────────────────────────────

def test_start_needs_the_warning_confirmed(app, client_for, admin, make_profile, devices):
	resp = start(client_for(admin), make_profile(admin), confirmed=False)
	assert (resp.status_code, resp.json["message"]) == (400, "Confirm the warning first")
	assert app.discovery.current() is None


def test_a_discovery_signs_in_with_the_profile_and_finds_the_devices(app, client_for, admin, make_profile,
                                                                     devices):
	"""Started: every address signed in to with the profile's login, the
	findings in the status; the starter sees it as theirs."""
	pid = make_profile(admin, username="netops", password="s3cret")
	resp = start(client_for(admin), pid)
	assert resp.status_code == 200
	assert resp.json["discovery"]["by"] == admin.username
	ended(app)
	assert sorted(d.ip for d in devices.calls) == ["10.20.0.1", "10.20.0.2", "10.20.0.3"]
	assert {(d.username, d.password, d.port) for d in devices.calls} == {("netops", "s3cret", 22)}
	status = client_for(admin).get("/inventory/discover/status").json["discovery"]
	assert status["phase"] == "done"
	assert status["mine"] is True
	assert sorted(e["address"] for e in status["events"] if e["kind"] == "finding") == \
		["10.20.0.1", "10.20.0.2", "10.20.0.3"]


def test_devices_in_inventory_are_never_signed_in_to(app, client_for, admin, make_user, make_profile,
                                                     make_device, devices):
	"""A device the admin sees at an address (own, global; by IP or by its
	looked-up address) is a line "in-inventory" with its label; another
	user's private device doesn't count."""
	make_device(admin, ip="10.20.0.1", label="edge-r1")
	make_device(make_user(), ip="10.20.0.2", label="theirs")
	with app.backend.postgres.get_session() as s:
		s.add(Inventory(ip="core1", resolved_ip="10.20.0.3", label="core1", port=22, device_type="cisco_xe",
		                user_id=make_user().id, is_global=True))
	start(client_for(admin), make_profile(admin))
	found = ended(app)
	assert [d.ip for d in devices.calls] == ["10.20.0.2"]
	lines = {(e["address"], e["reason"], e["detail"]) for e in found.scan.status()["events"] if e["kind"] == "line"}
	assert lines == {("10.20.0.1", "in-inventory", "edge-r1"), ("10.20.0.3", "in-inventory", "core1")}


def test_a_device_in_a_rollout_is_skipped_with_its_owner(app, client_for, admin, make_user, make_profile,
                                                         devices, monkeypatch):
	"""An address a rollout holds: "in-rollout", the owner named, never
	signed in to."""
	dana = make_user(username="dana")
	row = type("Row", (), {"user_id": str(dana.id), "named": lambda self, user: {"user": user}})()
	monkeypatch.setattr(app.orchestrator, "conflicts",
	                    lambda targets: {t: row for t in targets if t == "10.20.0.2:22"})
	start(client_for(admin), make_profile(admin))
	found = ended(app)
	assert sorted(d.ip for d in devices.calls) == ["10.20.0.1", "10.20.0.3"]
	assert [(e["address"], e["detail"]) for e in found.scan.status()["events"]
	        if e["kind"] == "line"] == [("10.20.0.2", "dana's rollout")]


def test_a_second_discovery_is_refused_while_one_runs(app, client_for, admin, make_user, make_profile, devices):
	"""409 naming who started the running one; after it ends a new one
	starts."""
	devices.gate.clear()
	start(client_for(admin), make_profile(admin))
	other = make_user(role="admin")
	resp = start(client_for(other), make_profile(other))
	assert resp.status_code == 409
	assert resp.json["message"].startswith(f"{admin.username}'s discovery is running (started ")
	devices.gate.set()
	ended(app)
	assert start(client_for(other), make_profile(other, label="p2")).status_code == 200


def test_no_discovery_starts_during_maintenance(app, client_for, admin, make_profile, devices):
	"""A database move under way: 503, and check says so too."""
	pid = make_profile(admin)
	assert app.maintenance.begin("a test", None)
	try:
		resp = start(client_for(admin), pid)
		assert (resp.status_code, resp.json["message"]) == (
			503, "NetRollout is moving to another database - start the discovery after the move.")
		check = client_for(admin).post("/inventory/discover/check", json={"addresses": "10.0.0.1",
		                                                                  "profile_id": str(pid)})
		assert check.json["refusal"] == resp.json["message"]
	finally:
		app.maintenance.end()
	assert app.discovery.current() is None


# ── status, continue, cancel ─────────────────────────────────────────────────

def test_status_gives_the_events_after_n(app, client_for, admin, make_profile, devices):
	start(client_for(admin), make_profile(admin))
	ended(app)
	client = client_for(admin)
	whole = client.get("/inventory/discover/status").json["discovery"]
	tail = client.get("/inventory/discover/status?since=3").json["discovery"]
	assert tail["events"] == whole["events"][3:]
	assert tail["seq"] == whole["seq"]
	assert client.get("/inventory/discover/status?since=x").status_code == 400


def test_status_without_a_discovery(client_for, admin, devices):
	assert client_for(admin).get("/inventory/discover/status").json == {"status": "ok", "discovery": None}


def test_another_admin_continues_a_paused_discovery(app, client_for, admin, make_user, make_profile, devices):
	"""Paused by failed sign-ins; another admin's Continue resumes it (the
	status not "mine" for them)."""
	for n in range(1, 9):
		devices.answers[f"10.20.0.{n}"] = AUTH
	start(client_for(admin), make_profile(admin), addresses="10.20.0.1-8")
	found = app.discovery.current()
	deadline = time.monotonic() + WAIT
	while found.scan.phase != "paused":
		assert time.monotonic() < deadline
		time.sleep(0.01)
	other = client_for(make_user(role="admin"))
	assert other.get("/inventory/discover/status").json["discovery"]["mine"] is False
	resp = other.post("/inventory/discover/continue")
	assert resp.status_code == 200
	assert found.scan.continues == 1


def test_another_admin_cancels_and_the_audit_names_them(app, client_for, admin, make_user, make_profile,
                                                        devices, session_scope):
	"""Cancel by another admin: the scan ends cancelled, and its one
	inventory.discover row (by the starter) says by whom, what was scanned
	and the warning confirmed - never the password."""
	devices.gate.clear()
	start(client_for(admin), make_profile(admin, label="Lab admins", password="s3cret"), addresses="10.20.0.1-12")
	other = make_user(role="admin")
	assert client_for(other).post("/inventory/discover/cancel").status_code == 200
	devices.gate.set()
	ended(app)
	assert client_for(other).post("/inventory/discover/cancel").status_code == 409
	(row,) = audit_rows(session_scope, "inventory.discover")
	assert (row.actor_username, row.object_label) == (admin.username, "10.20.0.1-12")
	detail = row.detail
	assert (detail["ended"], detail["cancelled_by"], detail["addresses"], detail["port"], detail["profile"]) == \
		("cancelled", other.username, 12, 22, "Lab admins")
	assert detail["warning_confirmed"] == {"addresses": 12, "from": ["192.0.2.10"]}
	assert "s3cret" not in str(detail)


def test_a_finished_discovery_is_audited_once(app, client_for, admin, make_profile, devices, session_scope):
	start(client_for(admin), make_profile(admin))
	ended(app)
	(row,) = audit_rows(session_scope, "inventory.discover")
	assert (row.detail["ended"], row.detail["counts"]["found"]) == ("finished", 3)


# ── create ───────────────────────────────────────────────────────────────────

def test_cards_become_devices_with_the_profile_linked_once(app, client_for, admin, make_profile, devices,
                                                           session_scope):
	"""Each card through Inventory's rules: the label and platform from the
	card, the discovery's profile linked, Global when asked (an admin);
	audited as inventory.create from discover. A card already created is
	refused the second time; a card without a platform or label isn't
	created."""
	pid = make_profile(admin)
	client = client_for(admin)
	start(client, pid)
	found = ended(app)
	ids = {f.address: f.id for f in found.scan.findings()}
	resp = client.post("/inventory/discover/create", json={"cards": [
		{"finding": ids["10.20.0.1"], "label": "core-sw1", "device_type": "cisco_xe", "is_global": True},
		{"finding": ids["10.20.0.2"], "label": "core-sw2", "device_type": "cisco_xe"},
		{"finding": ids["10.20.0.3"], "label": "", "device_type": "cisco_xe"},
		{"finding": ids["10.20.0.3"], "label": "x", "device_type": "nope"},
	]})
	assert resp.status_code == 200
	assert resp.json["results"] == [
		{"finding": ids["10.20.0.1"], "created": True, "label": "core-sw1", "warning": None},
		{"finding": ids["10.20.0.2"], "created": True, "label": "core-sw2", "warning": None},
		{"finding": ids["10.20.0.3"], "created": False, "message": "Give the device a label"},
		{"finding": ids["10.20.0.3"], "created": False, "message": "Choose the device's platform"},
	]
	with session_scope() as s:
		rows = {d.ip: (d.label, d.device_type, d.is_global, d.user_id) for d in s.query(Inventory)}
		links = {(c.device_id, c.profile_id) for c in s.query(DeviceCredential)}
		saved = {d.ip: d.id for d in s.query(Inventory)}
	assert rows == {"10.20.0.1": ("core-sw1", "cisco_xe", True, admin.id),
	                "10.20.0.2": ("core-sw2", "cisco_xe", False, admin.id)}
	assert links == {(saved["10.20.0.1"], pid), (saved["10.20.0.2"], pid)}
	again = client.post("/inventory/discover/create", json={"cards": [
		{"finding": ids["10.20.0.1"], "label": "core-sw1", "device_type": "cisco_xe"}]})
	assert again.json["results"] == [{"finding": ids["10.20.0.1"], "created": False,
	                                  "message": "Already added, or not one of this discovery's devices"}]
	creates = audit_rows(session_scope, "inventory.create")
	assert [(r.object_label, r.detail) for r in creates] == [
		("core-sw1", {"source": "discover", "is_global": True}),
		("core-sw2", {"source": "discover", "is_global": False})]


def test_a_shared_endpoint_is_a_warning_not_a_refusal(app, client_for, admin, make_user, make_profile,
                                                      make_device, devices):
	"""A device saved at the same ip:port between the scan and the create
	(Inventory allows that: NAT, VRFs) doesn't refuse the card: it is
	created, with Inventory's "already used by" warning."""
	pid = make_profile(admin)
	client = client_for(admin)
	start(client, pid, addresses="10.20.0.1")
	(finding,) = ended(app).scan.findings()
	make_device(admin, ip="10.20.0.1", label="added-meanwhile")
	resp = client.post("/inventory/discover/create", json={"cards": [
		{"finding": finding.id, "label": "core-sw1", "device_type": "cisco_xe"}]})
	(result,) = resp.json["results"]
	assert result["created"] is True
	assert "added-meanwhile" in result["warning"]


def test_only_the_starter_creates_devices(app, client_for, admin, make_user, make_profile, devices):
	"""Another admin can watch and cancel, but the devices (and the
	profile linked to them) are the starter's."""
	start(client_for(admin), make_profile(admin))
	(finding, *_) = ended(app).scan.findings()
	other = make_user(role="admin")
	resp = client_for(other).post("/inventory/discover/create", json={"cards": [
		{"finding": finding.id, "label": "x", "device_type": "cisco_xe"}]})
	assert (resp.status_code, resp.json["message"]) == (
		403, f"Only {admin.username} can add devices from their discovery")


def test_create_without_a_discovery(client_for, admin, devices):
	resp = client_for(admin).post("/inventory/discover/create",
	                              json={"cards": [{"finding": 1, "label": "x", "device_type": "cisco_xe"}]})
	assert (resp.status_code, resp.json["message"]) == (404, "The discovery's findings are gone - run it again")


def test_findings_are_dropped_an_hour_after_the_end(app, client_for, admin, make_profile, devices, monkeypatch):
	"""Kept KEEP_FOR seconds after the end, then gone."""
	start(client_for(admin), make_profile(admin))
	found = ended(app)
	assert app.discovery.current() is found
	later = found.scan.ended + datetime.timedelta(seconds=scan_module.KEEP_FOR + 1)
	monkeypatch.setattr(app.discovery, "_now", lambda: later)
	assert app.discovery.current() is None


# ── the Restart dialog ───────────────────────────────────────────────────────

def test_the_restart_dialog_names_a_running_discovery(app, client_for, admin, make_profile, devices):
	"""/admin/server/rollouts carries the running discovery (who, what);
	none once it has ended."""
	devices.gate.clear()
	start(client_for(admin), make_profile(admin))
	running = client_for(admin).get("/admin/server/rollouts").json["discovery"]
	assert (running["by"], running["entries"]) == (admin.username, ["10.20.0.1-3"])
	devices.gate.set()
	ended(app)
	assert client_for(admin).get("/admin/server/rollouts").json["discovery"] is None
