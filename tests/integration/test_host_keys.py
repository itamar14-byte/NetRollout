"""Devices' SSH host keys in the web app (src/host_keys.py and the routes
that sign in): the stored keys (learn once, refuse, accept replacing or
keeping, remove, reset), what each connection is checked against (off by
default; the stored keys; learn), what a connection saw recorded and
audited, and every path that signs in getting its check - a rollout, a
rollback, Test sign-in, Detect and Discover - with a refused key answered
as such. The check itself, against a real SSH server, is
tests/unit/rollout/test_host_keys.py."""
import uuid
from unittest.mock import patch

import paramiko
import pytest

from src.audit import Actor
from src.db.tables import AuditLog, HostKey, Inventory, SystemSetting
from src.discovery import scan as scan_module
from src.host_keys import HostKeyError, HostKeys
from src.rollout.detect import Detection
from src.rollout.host_keys import OFF, HostKeyRefused, fingerprint, key_line
from src.webapp.blueprints import discovery as discovery_routes
from tests.integration.test_rollouts_and_jobs import add_result

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

KEY_A = key_line(paramiko.RSAKey.generate(1024))
KEY_B = key_line(paramiko.RSAKey.generate(1024))
KEY_C = key_line(paramiko.ECDSAKey.generate())
WAIT = 10


@pytest.fixture
def checking(app, monkeypatch):
	"""Host keys checked (System Settings: trust on first use)."""
	monkeypatch.setattr(app.host_keys, "_mode", lambda: "trust_first_use")


def rows(session_scope, target=None):
	with session_scope() as s:
		q = s.query(HostKey)
		if target:
			q = q.filter_by(target=target)
		found = [(r.target, r.key, r.state, r.seen_by) for r in q.order_by(HostKey.first_seen)]
	return found


def audits(session_scope, action):
	with session_scope() as s:
		return [(a.actor_username, a.object_label, a.detail, a.success)
		        for a in s.query(AuditLog).filter_by(action=action).order_by(AuditLog.timestamp)]


def store(session_scope, target, key, state="accepted"):
	with session_scope() as s:
		if state == "accepted":
			HostKeys(s).learn(target, key, "seed")
		else:
			HostKeys(s).refuse(target, key, "seed")


# ── HostKeys: the stored keys ────────────────────────────────────────────────

def test_a_key_is_learned_once(app, session_scope):
	"""Two connections learning the same key at once store one row."""
	with session_scope() as s:
		assert HostKeys(s).learn("10.0.0.1:22", KEY_A, "dana (rollout)") is True
		assert HostKeys(s).learn("10.0.0.1:22", KEY_A, "dana (rollout)") is False
	assert rows(session_scope) == [("10.0.0.1:22", KEY_A, "accepted", "dana (rollout)")]


def test_accepting_a_refused_key_replaces_the_old_one_of_its_type(app, session_scope):
	"""A replaced device (RMA): its new RSA key accepted, the old RSA key
	removed; a key of another type stays."""
	store(session_scope, "10.0.0.1:22", KEY_A)
	store(session_scope, "10.0.0.1:22", KEY_C)
	store(session_scope, "10.0.0.1:22", KEY_B, state="refused")
	with session_scope() as s:
		new = s.query(HostKey).filter_by(key=KEY_B).one()
		HostKeys(s).accept(new.id, "admin", keep_others=False)
	assert {(k, st) for _, k, st, _ in rows(session_scope)} == {(KEY_B, "accepted"), (KEY_C, "accepted")}


def test_accepting_an_ha_units_key_keeps_the_other(app, session_scope):
	store(session_scope, "10.0.0.1:22", KEY_A)
	store(session_scope, "10.0.0.1:22", KEY_B, state="refused")
	with session_scope() as s:
		new = s.query(HostKey).filter_by(key=KEY_B).one()
		HostKeys(s).accept(new.id, "admin", keep_others=True)
	with session_scope() as s:
		assert HostKeys(s).accepted("10.0.0.1:22") == [KEY_A, KEY_B]


def test_accept_and_remove_refuse_what_isnt_there(app, session_scope):
	store(session_scope, "10.0.0.1:22", KEY_A)
	with session_scope() as s:
		row = s.query(HostKey).one()
		with pytest.raises(HostKeyError, match="is accepted already"):
			HostKeys(s).accept(row.id, "admin", keep_others=False)
		with pytest.raises(HostKeyError, match="isn't stored any more"):
			HostKeys(s).remove(uuid.uuid4())


def test_a_refusal_is_kept_and_counted_once_a_day(app, session_scope):
	with session_scope() as s:
		assert HostKeys(s).refuse("10.0.0.1:22", KEY_B, "dana (rollout)") is True
		assert HostKeys(s).refuse("10.0.0.1:22", KEY_B, "dana (rollout)") is False
	assert rows(session_scope) == [("10.0.0.1:22", KEY_B, "refused", "dana (rollout)")]


def test_reset_forgets_a_targets_keys_only(app, session_scope):
	store(session_scope, "10.0.0.1:22", KEY_A)
	store(session_scope, "10.0.0.2:22", KEY_A)
	with session_scope() as s:
		assert HostKeys(s).reset("10.0.0.1:22") == 1
	assert [t for t, *_ in rows(session_scope)] == ["10.0.0.2:22"]


# ── HostKeyPolicies: what a connection is checked against ───────────────────

def test_off_by_default(app):
	"""The setting's default: every connection unchecked (today's behaviour)."""
	assert app.host_keys.on is False
	assert app.host_keys.policy("10.0.0.1:22", Actor(None, "dana"), "rollout") is OFF


def test_the_setting_turns_it_on(app, session_scope):
	with session_scope() as s:
		s.merge(SystemSetting(key="host_key_mode", value="trust_first_use"))
	assert app.host_keys.on is True


def test_a_new_target_learns_and_a_known_one_verifies(app, session_scope, checking):
	actor = Actor(None, "dana")
	new = app.host_keys.policy("10.0.0.1:22", actor, "rollout")
	assert (new.known, new.learn) == ((), True)
	store(session_scope, "10.0.0.2:22", KEY_A)
	known = app.host_keys.policy("10.0.0.2:22", actor, "rollout")
	assert (known.known, known.learn) == ((KEY_A,), False)
	unsaved = app.host_keys.policy("10.0.0.1:22", actor, "detect", learn=False)
	assert unsaved.checks is False


def test_what_a_connection_saw_is_stored_and_audited(app, session_scope, make_user, checking):
	"""Learned: stored with who and how, audited host_key.learned; refused:
	kept for an admin, audited host_key.refused (failure) once."""
	dana = make_user(username="dana")
	actor = Actor(dana.id, "dana", "10.9.9.9")
	app.host_keys.policy("10.0.0.1:22", actor, "rollout").seen(KEY_A)
	assert rows(session_scope) == [("10.0.0.1:22", KEY_A, "accepted", "dana (rollout)")]
	refused = app.host_keys.policy("10.0.0.1:22", actor, "rollout").refused
	refused(KEY_B)
	refused(KEY_B)
	assert audits(session_scope, "host_key.learned") == [
		("dana", "10.0.0.1:22", {"fingerprint": fingerprint(KEY_A), "key_type": "ssh-rsa", "via": "rollout"}, True)]
	assert audits(session_scope, "host_key.refused") == [
		("dana", "10.0.0.1:22", {"fingerprint": fingerprint(KEY_B), "key_type": "ssh-rsa", "via": "rollout"}, False)]


def test_nothing_is_written_while_a_move_blocks_writes(app, session_scope, checking, monkeypatch):
	monkeypatch.setattr(app.host_keys, "_writes_blocked", lambda: True)
	app.host_keys.policy("10.0.0.1:22", Actor(None, "dana"), "rollout").seen(KEY_A)
	assert rows(session_scope) == []


# ── every path that signs in gets its check ──────────────────────────────────

@pytest.fixture
def operator(make_user, make_profile, make_device):
	user = make_user()
	profile = make_profile(user)
	device = make_device(user, ip="10.0.0.1", profile_id=profile)
	return type("Operator", (), {"user": user, "profile": profile, "device": device})


def test_a_rollout_checks_each_device(app, client_for, operator, session_scope, captured_submits, checking):
	store(session_scope, "10.0.0.1:22", KEY_A)
	client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.device)], "manual_commands": "hostname r9"})
	(call,) = captured_submits
	(device,) = call.devices
	assert (device.host_keys.known, device.host_keys.learn) == ((KEY_A,), False)


def test_a_rollout_unchecked_when_off(client_for, operator, captured_submits):
	client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.device)], "manual_commands": "hostname r9"})
	(call,) = captured_submits
	assert call.devices[0].host_keys is OFF


def test_a_rollback_checks_each_device(app, client_for, operator, session_scope, captured_submits, checking):
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}", json={"commands": "no hostname"})
	assert resp.status_code == 200, resp.json
	(call,) = captured_submits
	assert (call.devices[0].host_keys.known, call.devices[0].host_keys.learn) == ((), True)


def test_test_sign_in_checks_a_saved_device_and_learns(client_for, operator, checking):
	with patch("src.rollout.inputs.tcp_reachable", return_value=True), \
			patch("src.webapp.blueprints.security.NetmikoSession.connect") as connect:
		resp = client_for(operator.user).post(f"/security/{operator.profile}/test",
		                                      json={"device_id": str(operator.device)})
	assert resp.status_code == 200
	assert connect.call_args.args[0].host_keys.learn is True


def test_detect_verifies_a_stored_key_but_learns_nothing(client_for, operator, session_scope, checking):
	captured = []

	def detect(device):
		captured.append(device.host_keys)
		return Detection(platform="cisco_xe", detected="cisco_xe")

	with patch("src.rollout.inputs.tcp_reachable", return_value=True), \
			patch("src.webapp.blueprints.security.detect", side_effect=detect):
		client = client_for(operator.user)
		client.post(f"/security/{operator.profile}/detect", json={"ip": "10.0.0.7", "port": "22"})
		store(session_scope, "10.0.0.8:22", KEY_A)
		client.post(f"/security/{operator.profile}/detect", json={"ip": "10.0.0.8", "port": "22"})
	assert captured[0].checks is False
	assert (captured[1].known, captured[1].learn) == ((KEY_A,), False)


def test_a_refused_key_answers_409_with_the_reason(client_for, operator, checking):
	"""Test sign-in or Detect against a device whose key changed: 409 and
	why - not a 500."""
	with patch("src.rollout.inputs.tcp_reachable", return_value=True), \
			patch("src.webapp.blueprints.security.detect", side_effect=HostKeyRefused("10.0.0.9:22", KEY_B)):
		resp = client_for(operator.user).post(f"/security/{operator.profile}/detect",
		                                      json={"ip": "10.0.0.9", "port": "22"})
	assert resp.status_code == 409
	assert resp.json["message"].startswith("10.0.0.9:22: its SSH host key changed - not signed in to")


def test_discover_keeps_each_key_and_add_stores_it(app, client_for, make_user, make_profile, session_scope,
                                                   checking, monkeypatch):
	"""Discover's sign-ins are checked (a stored key verified, a new one
	kept on its finding); Add stores the key with the device, by the admin."""
	admin = make_user(role="admin", username="dana")
	monkeypatch.setattr(app.discovery, "knock", lambda address, port: "SSH-2.0-x")
	monkeypatch.setattr(app.discovery, "_current", None)
	monkeypatch.setattr(scan_module, "SIGN_IN_GAP", 0.0)
	monkeypatch.setattr(discovery_routes, "sending_from", lambda address, port: None)

	def detect(device):
		device.host_keys.seen(KEY_A)
		return Detection(platform="cisco_xe", detected="cisco_xe", hostname="sw1")

	monkeypatch.setattr(app.discovery, "detect", detect)
	client = client_for(admin)
	client.post("/inventory/discover/start", json={"addresses": "10.20.0.1", "port": "22",
	                                               "profile_id": str(make_profile(admin)), "confirmed": True})
	found = app.discovery.current()
	assert found.scan.wait(WAIT)
	(finding,) = found.scan.findings()
	assert finding.host_key == KEY_A
	assert rows(session_scope) == []                   # not stored by the scan
	resp = client.post("/inventory/discover/create", json={"cards": [
		{"finding": finding.id, "label": "sw1", "device_type": "cisco_xe"}]})
	assert resp.json["results"][0]["created"] is True
	assert rows(session_scope) == [("10.20.0.1:22", KEY_A, "accepted", "dana (discovery)")]


# ── Server Management's card, Inventory's badge ──────────────────────────────

def key_id(session_scope, key):
	with session_scope() as s:
		return s.query(HostKey).filter_by(key=key).one().id


def test_the_card_lists_targets_refused_first_with_their_devices(app, client_for, make_user, make_device,
                                                                  session_scope, checking):
	admin = make_user(role="admin")
	make_device(admin, ip="10.0.0.2", label="core-sw2")
	store(session_scope, "10.0.0.1:22", KEY_A)
	store(session_scope, "10.0.0.2:22", KEY_A)
	store(session_scope, "10.0.0.2:22", KEY_B, state="refused")
	state = client_for(admin).get("/admin/server/host-keys").json
	assert state["on"] is True
	assert [(t["target"], t["devices"], [k["state"] for k in t["keys"]]) for t in state["targets"]] == [
		("10.0.0.2:22", ["core-sw2"], ["refused", "accepted"]), ("10.0.0.1:22", [], ["accepted"])]
	assert state["targets"][0]["keys"][0]["fingerprint"] == fingerprint(KEY_B)


def test_an_admin_accepts_removes_and_resets_audited(client_for, make_user, session_scope):
	admin = make_user(role="admin")
	client = client_for(admin)
	store(session_scope, "10.0.0.1:22", KEY_A)
	store(session_scope, "10.0.0.1:22", KEY_B, state="refused")
	resp = client.post(f"/admin/server/host-keys/{key_id(session_scope, KEY_B)}/accept", json={})
	assert resp.status_code == 200
	assert [(k, st) for _, k, st, _ in rows(session_scope)] == [(KEY_B, "accepted")]   # replaced
	assert client.post(f"/admin/server/host-keys/{key_id(session_scope, KEY_B)}/accept", json={}).status_code == 409
	store(session_scope, "10.0.0.1:22", KEY_C, state="refused")
	assert client.post(f"/admin/server/host-keys/{key_id(session_scope, KEY_C)}/remove").status_code == 200
	assert client.post("/admin/server/host-keys/reset", json={"target": "10.0.0.1:22"}).status_code == 200
	assert rows(session_scope) == []
	assert client.post("/admin/server/host-keys/reset", json={"target": "10.0.0.1:22"}).status_code == 404
	assert [a[1] for a in audits(session_scope, "host_key.accepted")] == ["10.0.0.1:22"]
	assert audits(session_scope, "host_key.accepted")[0][2] == {"fingerprint": fingerprint(KEY_B), "kept_others": False}
	assert [a[2] for a in audits(session_scope, "host_key.removed")] == [
		{"fingerprint": fingerprint(KEY_C), "state": "refused"}]
	assert [a[2] for a in audits(session_scope, "host_key.reset")] == [{"removed": 1}]


def test_accepting_as_an_ha_unit_keeps_both(client_for, make_user, session_scope):
	admin = make_user(role="admin")
	store(session_scope, "10.0.0.1:22", KEY_A)
	store(session_scope, "10.0.0.1:22", KEY_B, state="refused")
	client_for(admin).post(f"/admin/server/host-keys/{key_id(session_scope, KEY_B)}/accept",
	                       json={"keep_others": True})
	assert sorted(st for _, _, st, _ in rows(session_scope)) == ["accepted", "accepted"]


@pytest.mark.parametrize("method, path", [
	("get", "/admin/server/host-keys"), ("post", f"/admin/server/host-keys/{uuid.uuid4()}/accept"),
	("post", f"/admin/server/host-keys/{uuid.uuid4()}/remove"), ("post", "/admin/server/host-keys/reset"),
])
def test_operators_are_refused(client_for, make_user, method, path):
	resp = getattr(client_for(make_user(), xhr=True), method)(path, json={"target": "x"})
	assert resp.status_code == 403


def test_inventory_marks_a_device_whose_key_was_refused(client_for, operator, make_device, session_scope):
	"""HOST KEY on the device at a target with a refused key, by its address
	or its hostname's resolved one; not on the others."""
	with session_scope() as s:
		s.query(Inventory).filter_by(id=operator.device).update({"ip": "core1", "resolved_ip": "10.0.0.1"})
	other = make_device(operator.user, ip="10.0.0.5", label="other")
	store(session_scope, "10.0.0.1:22", KEY_B, state="refused")
	page = client_for(operator.user).get("/inventory").get_data(as_text=True)
	assert page.count('class="key-refused-badge"') == 1
	assert str(other) in page
