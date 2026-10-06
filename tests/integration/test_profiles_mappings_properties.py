"""Security profiles (encryption at rest, delete guard, connection test),
variable mappings (validation, uniqueness, eligibility), and user-defined
properties (system-name shadowing)."""
import uuid
from unittest.mock import MagicMock, patch

import netmiko
import pytest

from src.db.tables import PropertyDefinition, SecurityProfile, VariableMapping, Inventory
from src.encryption import decrypt

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def profiles_of(session_scope, user):
	with session_scope() as s:
		rows = s.query(SecurityProfile).filter_by(user_id=user.id).all()
		s.expunge_all()
		return rows


# ── Security profiles ────────────────────────────────────────────────────────

def test_profile_secrets_are_encrypted_at_rest(client_for, make_user,
                                               session_scope):
	user = make_user()
	client_for(user).post("/security/create", data={
		"label": "core", "username": "netops", "password": "Sup3r-secret",
		"enable_secret": "en-able"})
	(profile,) = profiles_of(session_scope, user)
	assert profile.password_secret.startswith("gAAAAA")
	assert "Sup3r-secret" not in profile.password_secret
	assert decrypt(profile.password_secret) == "Sup3r-secret"
	assert decrypt(profile.enable_secret) == "en-able"


def test_quick_create_returns_id(client_for, make_user):
	resp = client_for(make_user()).post("/security/quick_create", json={
		"label": "core", "username": "u", "password": "p"})
	assert resp.json["status"] == "ok" and resp.json["id"]
	missing = client_for(make_user()).post("/security/quick_create",
	                                       json={"username": "u"})
	assert missing.status_code == 422


def test_profile_without_label_can_be_created(client_for, make_user,
                                              session_scope):
	user = make_user()
	client = client_for(user)
	resp = client.post("/security/quick_create", json={
		"username": "u", "password": "p"})
	assert resp.json["status"] == "ok" and resp.json["label"] == "u"
	client.post("/security/create", data={"username": "v", "password": "p"})
	labels = sorted((p.label, p.username) for p in profiles_of(session_scope, user))
	assert labels == [(None, "u"), (None, "v")]
	# the list page falls back to the username
	html = client.get("/security").get_data(as_text=True)
	assert "profile-title\">u<" in html.replace(" ", "")


def test_clearing_a_label_on_edit(client_for, make_user, make_profile, db_get):
	user = make_user()
	pid = make_profile(user, label="core")
	client_for(user).post(f"/security/{pid}/edit",
	                      data={"label": "", "username": "netops"})
	assert db_get(SecurityProfile, pid).label is None


def test_edit_keeps_password_when_left_blank(client_for, make_user,
                                             make_profile, db_get):
	user = make_user()
	pid = make_profile(user, password="original")
	client_for(user).post(f"/security/{pid}/edit", data={
		"label": "renamed", "username": "netops", "password": ""})
	p = db_get(SecurityProfile, pid)
	assert p.label == "renamed" and decrypt(p.password_secret) == "original"


def test_delete_blocked_while_devices_assigned(client_for, make_user,
                                               make_profile, make_device,
                                               db_get):
	user = make_user()
	pid = make_profile(user)
	make_device(user, profile_id=pid)
	client_for(user).post(f"/security/{pid}/delete")
	assert db_get(SecurityProfile, pid) is not None
	free = make_profile(user, label="free")
	client_for(user).post(f"/security/{free}/delete")
	assert db_get(SecurityProfile, free) is None


def test_profiles_are_private(client_for, make_user, make_profile, db_get):
	owner, other = make_user(), make_user()
	pid = make_profile(owner)
	client_for(other).post(f"/security/{pid}/edit", data={
		"username": "stolen", "password": "x"})
	client_for(other).post(f"/security/{pid}/delete")
	assert db_get(SecurityProfile, pid).username == "netops"


@pytest.mark.parametrize("tcp_ok,connect_error,expected", [
	(False, None, 503),
	(True, None, 200),
	(True, "auth", 401),
])
def test_connection_test(client_for, make_user, make_profile, make_device,
                         tcp_ok, connect_error, expected):
	user = make_user()
	pid = make_profile(user)
	dev = make_device(user)
	side_effect = (netmiko.NetMikoAuthenticationException("no")
	               if connect_error == "auth" else None)
	with patch("src.validation.tcp_reachable", return_value=tcp_ok), \
			patch("src.webapp.blueprints.security.ConnectHandler",
			      side_effect=side_effect, return_value=MagicMock()):
		resp = client_for(user).post(f"/security/{pid}/test",
		                             json={"device_id": str(dev)})
	assert resp.status_code == expected


# ── Variable mappings ────────────────────────────────────────────────────────

def mappings_of(session_scope, user):
	with session_scope() as s:
		rows = s.query(VariableMapping).filter_by(user_id=user.id).all()
		s.expunge_all()
		return rows


def test_mapping_create_normalises_token(client_for, make_user, session_scope):
	user = make_user()
	client_for(user).post("/mappings/create", data={
		"token_inner": "hostname", "property_name": "hostname"})
	(m,) = mappings_of(session_scope, user)
	assert m.token == "$$HOSTNAME$$" and m.index is None


@pytest.mark.parametrize("form", [
	{"token_inner": "bad token", "property_name": "hostname"},
	{"token_inner": "X", "property_name": "not_a_property"},
	{"token_inner": "X", "property_name": "hostname", "index": "1"},
])
def test_invalid_mappings_rejected(client_for, make_user, session_scope, form):
	user = make_user()
	client_for(user).post("/mappings/create", data=form)
	assert mappings_of(session_scope, user) == []


def test_duplicate_token_rejected_per_user(client_for, make_user,
                                           session_scope):
	a, b = make_user(), make_user()
	form = {"token_inner": "SITE", "property_name": "site"}
	client_for(a).post("/mappings/create", data=form)
	client_for(a).post("/mappings/create", data=form)
	client_for(b).post("/mappings/create", data=form)  # other user: allowed
	assert len(mappings_of(session_scope, a)) == 1
	assert len(mappings_of(session_scope, b)) == 1


def test_quick_create_and_edit(client_for, make_user, db_get):
	user = make_user()
	client = client_for(user)
	resp = client.post("/mappings/quick_create", json={
		"token_inner": "vrf", "property_name": "vrfs", "index": 0})
	assert resp.json["token"] == "$$VRF$$"
	mid = resp.json["id"]
	client.post(f"/mappings/{mid}/edit", data={
		"token_inner": "vrf2", "property_name": "vrfs", "index": "1"})
	m = db_get(VariableMapping, uuid.UUID(mid))
	assert (m.token, m.index) == ("$$VRF2$$", 1)


def test_bulk_assign_checks_eligibility(client_for, make_user, make_device,
                                        make_mapping, session_scope):
	user = make_user()
	ok = make_device(user, ip="10.0.0.1", var_maps={"hostname": "r1"})
	missing = make_device(user, ip="10.0.0.2", var_maps={})
	mid = make_mapping(user)
	client_for(user).post("/mappings/bulk_assign", json={
		"mapping_id": str(mid), "device_ids": [str(ok), str(missing)]})
	with session_scope() as s:
		assigned = {d.id for d in s.get(VariableMapping, mid).devices}
	assert assigned == {ok}


# ── Properties ───────────────────────────────────────────────────────────────

def test_property_lifecycle(client_for, make_user, db_get):
	client = client_for(make_user())
	resp = client.post("/properties/create", json={
		"name": "Rack Unit", "label": "Rack Unit", "is_list": False})
	assert resp.json["name"] == "rack_unit"
	pid = uuid.UUID(resp.json["id"])
	client.post(f"/properties/{pid}/edit", json={"label": "Rack", "is_list": True})
	prop = db_get(PropertyDefinition, pid)
	assert (prop.label, prop.is_list) == ("Rack", True)
	client.post(f"/properties/{pid}/delete")
	assert db_get(PropertyDefinition, pid) is None


def test_property_cannot_shadow_system_or_duplicate(client_for, make_user):
	client = client_for(make_user())
	assert client.post("/properties/create", json={
		"name": "hostname", "label": "Host"}).json["status"] == "error"
	client.post("/properties/create", json={"name": "rack", "label": "Rack"})
	assert client.post("/properties/quick_create", json={
		"name": "rack", "label": "Rack"}).json["status"] == "error"


def test_pages_render(client_for, make_user):
	client = client_for(make_user())
	for path in ("/security", "/mappings", "/properties"):
		assert client.get(path).status_code == 200, path


def test_mappings_on_user_defined_properties(client_for, make_user,
                                             make_device, session_scope):
	# custom properties used to be rejected: the validator only knew the
	# nine built-in names
	user = make_user()
	client = client_for(user)
	client.post("/properties/create", json={"name": "rack", "label": "Rack"})
	client.post("/properties/create", json={"name": "uplinks",
	                                        "label": "Uplinks", "is_list": True})
	client.post("/mappings/create", data={"token_inner": "RACK",
	                                      "property_name": "rack"})
	resp = client.post("/mappings/quick_create", json={
		"token_inner": "UP1", "property_name": "uplinks", "index": 1})
	assert resp.json["status"] == "ok"
	bad = client.post("/mappings/quick_create", json={
		"token_inner": "RACK0", "property_name": "rack", "index": 0})
	assert bad.json["status"] == "error"  # rack isn't a list
	tokens = {m.token for m in mappings_of(session_scope, user)}
	assert tokens == {"$$RACK$$", "$$UP1$$"}
	# and they bind through drag-assign like built-ins
	dev = make_device(user, var_maps={"rack": "R12"})
	rack = next(m for m in mappings_of(session_scope, user)
	            if m.token == "$$RACK$$")
	client.post("/mappings/bulk_assign", json={"mapping_id": str(rack.id),
	                                           "device_ids": [str(dev)]})
	with session_scope() as s:
		assert [d.id for d in s.get(VariableMapping, rack.id).devices] == [dev]


def test_bulk_assign_removes_only_own_bindings(client_for, make_user,
                                               make_device, make_mapping,
                                               session_scope, db_get):
	admin, a, b = make_user(role="admin"), make_user(), make_user()
	core = make_device(admin, is_global=True, var_maps={"hostname": "core"})
	mine = make_device(a, ip="10.0.0.2", var_maps={"hostname": "r2"})
	map_a = make_mapping(a, devices=(core, mine))
	map_b = make_mapping(b, devices=(core,))
	client = client_for(a)
	# remove-only save (no device_ids) is accepted
	resp = client.post("/mappings/bulk_assign", json={
		"mapping_id": str(map_a), "remove_ids": [str(core), "not-a-uuid"]})
	assert resp.json["status"] == "ok"
	with session_scope() as s:
		assert [d.id for d in s.get(VariableMapping, map_a).devices] == [mine]
		# B's binding on the same global device is untouched
		assert [d.id for d in s.get(VariableMapping, map_b).devices] == [core]
	# unassigning never deletes the device itself
	assert db_get(Inventory, core) is not None
	# add and remove in one call
	client.post("/mappings/bulk_assign", json={
		"mapping_id": str(map_a), "device_ids": [str(core)],
		"remove_ids": [str(mine)]})
	with session_scope() as s:
		assert [d.id for d in s.get(VariableMapping, map_a).devices] == [core]
	assert db_get(Inventory, mine) is not None
	# B can't unbind A's mapping
	client_for(b).post("/mappings/bulk_assign", json={
		"mapping_id": str(map_a), "remove_ids": [str(core)]})
	with session_scope() as s:
		assert [d.id for d in s.get(VariableMapping, map_a).devices] == [core]
	# both lists empty is still rejected
	assert client.post("/mappings/bulk_assign", json={
		"mapping_id": str(map_a)}).json["status"] == "error"
