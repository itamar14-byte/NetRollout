"""Attribute values per user: a device's system values are its own (var_maps,
shared, set by who may edit it); a custom property's values are each user's
(device_attributes) - set by anyone who sees the device, read by their own
mappings and rollouts only, removed with the property, the device or the
user."""
import datetime as dt
import io
import json
import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.db.tables import (DeviceAttribute, DeviceResult, Inventory, PropertyDefinition,
                           User)

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

FORM = {"label": "CORE-X", "ip": "10.50.0.1", "port": "22",
        "device_type": "cisco_ios"}


@pytest.fixture
def world(make_user, make_profile, make_device, client_for):
	"""An admin's global device CORE-X (hostname core-x, a profile), operators b
	and c each with their own property "rack", and b's private device."""
	admin = make_user(role="admin")
	b, c = make_user(), make_user()
	prof = make_profile(admin)
	core = make_device(admin, ip="10.50.0.1", label="CORE-X", is_global=True,
	                   profile_id=prof, var_maps={"hostname": "core-x"})
	b_own = make_device(b, ip="10.60.0.1", label="B-OWN",
	                    profile_id=make_profile(b))
	for user in (b, c):
		client_for(user).post("/properties/create",
		                      json={"name": "rack", "label": "Rack"})
	return SimpleNamespace(admin=admin, b=b, c=c, prof=prof, core=core,
	                       b_own=b_own)


def values_of(session_scope, device_id):
	"""{(username, name): value} of every user's custom values on a device."""
	with session_scope() as s:
		return {(s.get(User, a.user_id).username, a.name): a.value
		        for a in s.query(DeviceAttribute).filter_by(device_id=device_id)}


def var_maps_of(session_scope, device_id):
	with session_scope() as s:
		return s.get(Inventory, device_id).var_maps


def set_mine(client, device_id, **fields):
	"""POST the read-only modal's form: attr_<name> fields (and mapping_ids)."""
	data = {k if k == "mapping_ids" else f"attr_{k}": v for k, v in fields.items()}
	return client.post(f"/inventory/{device_id}/attributes", data=data)


def test_two_users_keep_their_own_value_of_a_same_named_property(
		world, client_for, session_scope, make_mapping, captured_submits):
	"""b and c each set their own "rack" on the same global device: both values
	are kept; each user's mapping binds, their rollout substitutes and their
	Inventory and Mappings pages show only their own value."""
	for user, rack in ((world.b, "B-RACK-1"), (world.c, "C-RACK-9")):
		mapping = make_mapping(user, token="RACK", prop="rack")
		set_mine(client_for(user), world.core, rack=rack,
		         mapping_ids=[str(mapping)])
	assert values_of(session_scope, world.core) == {
		(world.b.username, "rack"): "B-RACK-1",
		(world.c.username, "rack"): "C-RACK-9"}
	assert var_maps_of(session_scope, world.core) == {"hostname": "core-x"}

	for user, mine, theirs in ((world.b, "B-RACK-1", "C-RACK-9"),
	                           (world.c, "C-RACK-9", "B-RACK-1")):
		client = client_for(user)
		for page in ("/inventory", "/mappings"):
			html = client.get(page).get_data(as_text=True)
			assert mine in html and theirs not in html, page
		captured_submits.clear()
		client.post("/rollout/start", data={
			"device_ids": [str(world.core)], "manual_commands": "rack $$RACK$$"})
		(call,) = captured_submits
		(device,) = call.devices
		assert device.var_map_subs == {"$$RACK$$": ("rack", None)}
		assert device.extra == {"hostname": "core-x", "rack": mine}


def test_an_edit_keeps_other_users_values_and_ignores_unknown_fields(
		world, client_for, session_scope):
	"""An admin's edit of the global device saves the system values and the
	admin's own custom value, ignores a field naming no property of theirs, and
	leaves b's value alone."""
	set_mine(client_for(world.b), world.core, rack="B-RACK-1")
	admin = client_for(world.admin)
	admin.post("/properties/create", json={"name": "rack", "label": "Rack"})
	admin.post(f"/inventory/{world.core}/edit", data={
		**FORM, "sec_profile_id": str(world.prof), "is_global": "on",
		"attr_hostname": "core-y", "attr_rack": "A-RACK", "attr_bogus": "x"})
	assert var_maps_of(session_scope, world.core) == {"hostname": "core-y"}
	assert values_of(session_scope, world.core) == {
		(world.b.username, "rack"): "B-RACK-1",
		(world.admin.username, "rack"): "A-RACK"}


def test_an_edit_without_a_field_keeps_its_value_and_a_blank_one_removes_it(
		world, client_for, session_scope):
	"""The owner's edit changes only the attributes the form sends: a blank field
	removes the value (system or custom), a field not sent keeps it."""
	client = client_for(world.b)
	client.post("/properties/create", json={"name": "row", "label": "Row"})
	form = {**FORM, "label": "B-OWN", "ip": "10.60.0.1"}
	client.post(f"/inventory/{world.b_own}/edit", data={
		**form, "attr_hostname": "b1", "attr_site": "lab",
		"attr_rack": "R1", "attr_row": "7"})
	client.post(f"/inventory/{world.b_own}/edit", data={
		**form, "attr_site": "", "attr_row": ""})
	assert var_maps_of(session_scope, world.b_own) == {"hostname": "b1"}
	assert values_of(session_scope, world.b_own) == {
		(world.b.username, "rack"): "R1"}


def test_an_operator_sets_only_their_own_values_on_a_device_they_see(
		world, client_for, session_scope, db_get):
	"""On a global device b can't edit, b's form saves b's own custom value only:
	the device (its system values, label) is unchanged, an unknown field is
	ignored, a blank one removes b's value; a device b can't see is 404."""
	client = client_for(world.b)
	resp = set_mine(client, world.core, rack="B-RACK-1", hostname="evil",
	                label="X", nope="1")
	assert resp.status_code == 302
	assert var_maps_of(session_scope, world.core) == {"hostname": "core-x"}
	assert db_get(Inventory, world.core).label == "CORE-X"
	assert values_of(session_scope, world.core) == {
		(world.b.username, "rack"): "B-RACK-1"}

	set_mine(client, world.core, rack="")
	assert values_of(session_scope, world.core) == {}

	resp = set_mine(client_for(world.c), world.b_own, rack="C-RACK")
	assert resp.status_code == 404
	assert values_of(session_scope, world.b_own) == {}


def test_a_rollback_uses_the_job_owners_values(world, client_for, session_scope,
                                               make_mapping, captured_submits):
	"""An admin's rollback of b's job on the global device substitutes b's value
	of "rack", not the admin's own."""
	admin = client_for(world.admin)
	admin.post("/properties/create", json={"name": "rack", "label": "Rack"})
	set_mine(admin, world.core, rack="A-RACK")
	mapping = make_mapping(world.b, token="RACK", prop="rack")
	set_mine(client_for(world.b), world.core, rack="B-RACK-1",
	         mapping_ids=[str(mapping)])
	job, now = uuid.uuid4(), dt.datetime.now()
	with session_scope() as s:
		s.add(DeviceResult(user_id=world.b.id, job_id=job, started_at=now,
		                   completed_at=now, device_ip="10.50.0.1",
		                   device_port=22, device_type="cisco_ios",
		                   commands_sent=1, status="success"))
	resp = admin.post(f"/rollout/rollback/{job}",
	                  json={"commands": "no rack $$RACK$$"})
	assert resp.json["status"] == "ok"
	(call,) = captured_submits
	(device,) = call.devices
	assert device.extra["rack"] == "B-RACK-1"


def test_the_csv_import_writes_custom_columns_as_the_importers(
		world, client_for, session_scope):
	"""A CSV import by b puts the system column in the device's var_maps and the
	custom column in b's own values - not c's, who has a property of that name
	too."""
	csv = "ip,device_type,port,label,hostname,rack\n10.7.7.1,cisco_ios,22,r7,r7.lab,R7\n"
	client_for(world.b).post("/inventory/import_csv", data={
		"csv_file": (io.BytesIO(csv.encode()), "devices.csv")},
		content_type="multipart/form-data")
	with session_scope() as s:
		device_id = s.query(Inventory.id).filter_by(label="r7").scalar()
	assert var_maps_of(session_scope, device_id) == {"hostname": "r7.lab"}
	assert values_of(session_scope, device_id) == {(world.b.username, "rack"): "R7"}


def test_deleting_a_property_deletes_that_users_values_only(
		world, client_for, session_scope):
	"""b deleting their property "rack" removes b's values of it on every device;
	c's value on the shared device stays."""
	set_mine(client_for(world.b), world.core, rack="B-RACK-1")
	set_mine(client_for(world.b), world.b_own, rack="B-RACK-2")
	set_mine(client_for(world.c), world.core, rack="C-RACK-9")
	with session_scope() as s:
		prop_id = s.execute(select(PropertyDefinition.id).filter_by(
			user_id=world.b.id, name="rack")).scalar()
	client_for(world.b).post(f"/properties/{prop_id}/delete")
	assert values_of(session_scope, world.core) == {
		(world.c.username, "rack"): "C-RACK-9"}
	assert values_of(session_scope, world.b_own) == {}


def test_deleting_a_device_or_a_user_leaves_no_values(
		world, client_for, session_scope, make_user, make_profile, make_device):
	"""Deleting a device removes every user's values on it; deleting a user removes
	theirs - and deleting an admin whose global device carries other users'
	values removes the device and those values, nothing blocked."""
	set_mine(client_for(world.b), world.b_own, rack="B-RACK-2")
	set_mine(client_for(world.b), world.core, rack="B-RACK-1")
	set_mine(client_for(world.c), world.core, rack="C-RACK-9")

	client_for(world.b).post(f"/inventory/{world.b_own}/delete")
	assert values_of(session_scope, world.b_own) == {}

	admin2 = make_user(role="admin")
	other_global = make_device(admin2, ip="10.70.0.1", is_global=True,
	                           profile_id=make_profile(admin2))
	set_mine(client_for(world.c), other_global, rack="C-RACK-2")
	client_for(world.admin).post(f"/admin/users/{world.b.id}/delete")
	client_for(world.admin).post(f"/admin/users/{admin2.id}/delete")
	with session_scope() as s:
		assert s.get(User, admin2.id) is None and s.get(Inventory, other_global) is None
		rows = {(a.user_id, a.device_id) for a in s.query(DeviceAttribute)}
	assert rows == {(world.c.id, world.core)}


def test_making_a_device_local_drops_the_other_users_values(world, client_for,
                                                             session_scope):
	"""A global device made local: the other users can't see it any more - their values
	on it go with their mapping bindings (they were left hidden, coming back if it went
	global again); the owner's stay."""
	admin = client_for(world.admin)
	admin.post("/properties/create", json={"name": "rack", "label": "Rack"})
	set_mine(client_for(world.b), world.core, rack="B-RACK")
	admin.post(f"/inventory/{world.core}/edit", data={
		**FORM, "sec_profile_id": str(world.prof), "is_global": "on", "attr_rack": "A-RACK"})
	admin.post(f"/inventory/{world.core}/edit", data={
		**FORM, "sec_profile_id": str(world.prof), "attr_rack": "A-RACK"})
	assert values_of(session_scope, world.core) == {(world.admin.username, "rack"): "A-RACK"}


def page_token_states(client):
	"""The New Rollout page's token data (TOKEN_STATES, rendered with |tojson)."""
	html = client.get("/rollout/new").get_data(as_text=True)
	found = re.search(r"const TOKEN_STATES = (.*?);\s*$", html, re.M)
	assert found, "the page has no token data"
	return json.loads(found.group(1))


def test_the_new_rollout_page_gives_each_device_the_users_own_tokens(
		world, client_for, make_mapping):
	"""The New Rollout page tells its script, per device, the tokens the user
	bound on it and why each can't be filled in (null: it can) - from the
	device's system values and the user's own custom ones. Another user's
	mapping on the same global device, and another user's value of a property
	of the same name, never reach it."""
	set_mine(client_for(world.c), world.core, rack="C-RACK-1")
	make_mapping(world.b, token="HOST", prop="hostname", devices=[world.core])
	make_mapping(world.b, token="RACK", prop="rack", devices=[world.core])
	make_mapping(world.c, token="CRACK", prop="rack", devices=[world.core])

	assert page_token_states(client_for(world.b)) == {
		str(world.core): {"$$HOST$$": None, "$$RACK$$": "no value for 'rack'"}}
	assert page_token_states(client_for(world.c)) == {
		str(world.core): {"$$CRACK$$": None}}
