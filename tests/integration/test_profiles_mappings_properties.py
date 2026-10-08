"""Security profiles (encryption at rest, delete guard, connection test),
variable mappings (validation, uniqueness, eligibility), and user-defined
properties (system-name shadowing)."""
import uuid
from unittest.mock import MagicMock, patch

import netmiko
import pytest

from src.db.tables import AuditLog, PropertyDefinition, SecurityProfile, VariableMapping, Inventory
from src.encryption import decrypt

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def profiles_of(session_scope, user):
	"""The user's security profiles, detached from the session."""
	with session_scope() as s:
		rows = s.query(SecurityProfile).filter_by(user_id=user.id).all()
		s.expunge_all()
		return rows


# ── Security profiles ────────────────────────────────────────────────────────

def test_profile_secrets_are_encrypted_at_rest(client_for, make_user,
                                               session_scope):
	"""A new profile's password and enable secret are stored encrypted (Fernet)
	and decrypt back to what was typed."""
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
	"""Quick create returns ok with the new id; without a password it is 422."""
	resp = client_for(make_user()).post("/security/quick_create", json={
		"label": "core", "username": "u", "password": "p"})
	assert resp.json["status"] == "ok" and resp.json["id"]
	missing = client_for(make_user()).post("/security/quick_create",
	                                       json={"username": "u"})
	assert missing.status_code == 422


def test_profile_without_label_can_be_created(client_for, make_user,
                                              session_scope):
	"""A profile can be created without a label, by quick create (which answers
	the username as its label) and by the form; the list page shows the
	username instead."""
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
	"""Saving a profile with an empty label clears the label."""
	user = make_user()
	pid = make_profile(user, label="core")
	client_for(user).post(f"/security/{pid}/edit",
	                      data={"label": "", "username": "netops"})
	assert db_get(SecurityProfile, pid).label is None


def test_edit_keeps_password_when_left_blank(client_for, make_user,
                                             make_profile, db_get):
	"""Editing a profile with the password blank keeps the stored password."""
	user = make_user()
	pid = make_profile(user, password="original")
	client_for(user).post(f"/security/{pid}/edit", data={
		"label": "renamed", "username": "netops", "password": ""})
	p = db_get(SecurityProfile, pid)
	assert p.label == "renamed" and decrypt(p.password_secret) == "original"


def test_delete_blocked_while_devices_assigned(client_for, make_user,
                                               make_profile, make_device,
                                               db_get):
	"""A profile with a device assigned isn't deleted; one without is."""
	user = make_user()
	pid = make_profile(user)
	make_device(user, profile_id=pid)
	client_for(user).post(f"/security/{pid}/delete")
	assert db_get(SecurityProfile, pid) is not None
	free = make_profile(user, label="free")
	client_for(user).post(f"/security/{free}/delete")
	assert db_get(SecurityProfile, free) is None


def test_profiles_are_private(client_for, make_user, make_profile, db_get):
	"""Another user can't edit or delete someone's profile."""
	owner, other = make_user(), make_user()
	pid = make_profile(owner)
	client_for(other).post(f"/security/{pid}/edit", data={
		"username": "stolen", "password": "x"})
	client_for(other).post(f"/security/{pid}/delete")
	kept = db_get(SecurityProfile, pid)
	assert kept is not None and kept.username == "netops"


@pytest.mark.parametrize("tcp_ok,connect_error,expected", [
	(False, None, 503),
	(True, None, 200),
	(True, "auth", 401),
])
def test_connection_test(client_for, make_user, make_profile, make_device,
                         tcp_ok, connect_error, expected):
	"""The profile's connection test answers 503 for an unreachable device, 200
	when the connection works, 401 when authentication fails."""
	user = make_user()
	pid = make_profile(user)
	dev = make_device(user)
	side_effect = (netmiko.NetMikoAuthenticationException("no")
	               if connect_error == "auth" else None)
	with patch("src.rollout.inputs.tcp_reachable", return_value=tcp_ok), \
			patch("src.webapp.blueprints.security.ConnectHandler",
			      side_effect=side_effect, return_value=MagicMock()):
		resp = client_for(user).post(f"/security/{pid}/test",
		                             json={"device_id": str(dev)})
	assert resp.status_code == expected


# ── Variable mappings ────────────────────────────────────────────────────────

def mappings_of(session_scope, user):
	"""The user's variable mappings, detached from the session."""
	with session_scope() as s:
		rows = s.query(VariableMapping).filter_by(user_id=user.id).all()
		s.expunge_all()
		return rows


def test_mapping_create_normalises_token(client_for, make_user, session_scope):
	"""A mapping's token is stored as $$UPPERCASE$$, with no index."""
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
	"""An invalid mapping isn't saved (cases: a token with a space, an unknown
	property, an index on a property that isn't a list)."""
	user = make_user()
	client_for(user).post("/mappings/create", data=form)
	assert mappings_of(session_scope, user) == []


def test_duplicate_token_rejected_per_user(client_for, make_user,
                                           session_scope):
	"""A user can't create the same token twice; another user can have it."""
	a, b = make_user(), make_user()
	form = {"token_inner": "SITE", "property_name": "site"}
	client_for(a).post("/mappings/create", data=form)
	client_for(a).post("/mappings/create", data=form)
	client_for(b).post("/mappings/create", data=form)  # other user: allowed
	assert len(mappings_of(session_scope, a)) == 1
	assert len(mappings_of(session_scope, b)) == 1


def test_quick_create_and_edit(client_for, make_user, db_get):
	"""Quick create returns the token; an edit renames it and changes the
	index."""
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
	"""Bulk assign binds the mapping only to devices that have its property."""
	user = make_user()
	ok = make_device(user, ip="10.0.0.1", var_maps={"hostname": "r1"})
	missing = make_device(user, ip="10.0.0.2", var_maps={})
	mid = make_mapping(user)
	client_for(user).post("/mappings/bulk_assign", json={
		"mapping_id": str(mid), "device_ids": [str(ok), str(missing)]})
	with session_scope() as s:
		assigned = {d.id for d in s.get(VariableMapping, mid).devices}
	assert assigned == {ok}


def test_bulk_assign_audits_what_was_assigned(client_for, make_user, make_device,
                                              make_mapping, session_scope):
	"""The bulk-assign audit counts the devices actually assigned (1 of the 2
	sent - the other lacks the property), not the ids sent."""
	user = make_user()
	ok = make_device(user, ip="10.0.0.1", var_maps={"hostname": "r1"})
	missing = make_device(user, ip="10.0.0.2", var_maps={})
	mid = make_mapping(user)
	client_for(user).post("/mappings/bulk_assign", json={
		"mapping_id": str(mid), "device_ids": [str(ok), str(missing)]})
	with session_scope() as s:
		(row,) = s.query(AuditLog).filter_by(action="mapping.bulk_assign").all()
		assert (row.detail["count"], row.detail["removed"]) == (1, 0)


def test_a_mapping_index_that_isnt_a_number_is_refused_in_words(
		client_for, make_user, session_scope):
	"""A mapping whose index isn't a number is refused with the reason - on
	the page's form (flashed, nothing saved) and in the quick create (422) -
	instead of a server error."""
	user = make_user()
	client = client_for(user)
	resp = client.post("/mappings/create", data={
		"token_inner": "vrf", "property_name": "vrfs", "index": "two"})
	assert resp.headers["Location"] == "/mappings"
	with client.session_transaction() as s:
		assert [m for _, m in s["_flashes"]] == ["The index is a number."]
	quick = client.post("/mappings/quick_create", json={
		"token_inner": "vrf", "property_name": "vrfs", "index": "two"})
	assert (quick.status_code, quick.json["message"]) == (422, "The index is a number.")
	assert mappings_of(session_scope, user) == []


# ── Properties ───────────────────────────────────────────────────────────────

def test_property_lifecycle(client_for, make_user, db_get):
	"""A property is created (name normalised to rack_unit), edited (label,
	list) and deleted."""
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
	"""A property can't take a built-in name (hostname) or an existing one's
	name, by create or quick create."""
	client = client_for(make_user())
	assert client.post("/properties/create", json={
		"name": "hostname", "label": "Host"}).json["status"] == "error"
	client.post("/properties/create", json={"name": "rack", "label": "Rack"})
	assert client.post("/properties/quick_create", json={
		"name": "rack", "label": "Rack"}).json["status"] == "error"


@pytest.mark.parametrize("route", ["/properties/create", "/properties/quick_create"])
@pytest.mark.parametrize("changes, message", [
	({"name": "<b>x</b>"}, "letters, digits and _"),
	({"name": "rack-unit"}, "letters, digits and _"),
	({"name": "9rack"}, "letters, digits and _"),
	({"icon": "bi-tag\" onmouseover=\"x"}, "Bootstrap Icons class"),
	({"icon": "fa-rack"}, "Bootstrap Icons class"),
	({"label": "L" * 65}, "at most 64"),
	({"name": "r" * 65}, "at most 64"),
])
def test_a_property_must_be_a_safe_key_and_icon(client_for, make_user, session_scope,
                                                route, changes, message):
	"""A property's name (the $$TOKEN$$ dict key) is letters, digits and _ starting
	with a letter (after the usual normalisation), its icon a Bootstrap Icons class
	(bi-...), name and label at most 64 characters - else refused with the reason,
	nothing stored."""
	client = client_for(make_user())
	resp = client.post(route, json={"name": "rack", "label": "Rack", "icon": "bi-hdd", **changes})
	assert resp.json["status"] == "error" and message in resp.json["message"], resp.json
	with session_scope() as s:
		assert s.query(PropertyDefinition).count() == 0


def test_a_property_icon_edit_is_checked_too(client_for, make_user, db_get):
	"""Editing a property can't set an icon that isn't a Bootstrap Icons class;
	a good one is saved."""
	client = client_for(make_user())
	pid = uuid.UUID(client.post("/properties/create", json={"name": "rack", "label": "Rack"}).json["id"])
	resp = client.post(f"/properties/{pid}/edit", json={"label": "Rack", "icon": "x onclick=y"})
	assert resp.json["status"] == "error" and "Bootstrap Icons class" in resp.json["message"]
	assert db_get(PropertyDefinition, pid).icon == "bi-tag"
	client.post(f"/properties/{pid}/edit", json={"label": "Rack", "icon": "bi-hdd-rack"})
	assert db_get(PropertyDefinition, pid).icon == "bi-hdd-rack"


def test_pages_render(client_for, make_user):
	"""The security profiles, mappings and properties pages render."""
	client = client_for(make_user())
	for path in ("/security", "/mappings", "/properties"):
		assert client.get(path).status_code == 200, path


def test_mappings_on_user_defined_properties(client_for, make_user,
                                             make_device, session_scope):
	"""Custom properties used to be rejected (the validator only knew the nine
	built-in names): mappings on user-defined properties are accepted, an
	index only on a list one, and they bind through drag-assign like built-ins."""
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
	"""Bulk assign's remove_ids unbinds only from the user's own mapping (another
	user's binding on the same global device stays), never deletes a device,
	can add and remove in one call; another user can't unbind it, and a call
	with neither list is refused."""
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
