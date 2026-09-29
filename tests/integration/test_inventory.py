"""Inventory CRUD, CSV import, bulk profile assign, and global devices
(visibility, admin-only edits, per-user mappings, credential isolation)."""
import io
import re
from unittest.mock import patch

import pytest

from src.db.tables import AuditLog, Inventory

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

FORM = {"label": "edge-1", "ip": "10.1.1.1", "port": "22",
        "device_type": "cisco_ios"}


def flashes(client):
	with client.session_transaction() as s:
		return [m for _, m in s.get("_flashes", [])]


def device_by_label(session_scope, label):
	with session_scope() as s:
		d = s.query(Inventory).filter_by(label=label).first()
		if d:
			s.expunge(d)
		return d


def mapping_owners(session_scope, device_id):
	with session_scope() as s:
		return sorted(str(m.user_id) for m in s.get(Inventory, device_id).var_mappings)


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_create_edit_delete_own_device(client_for, make_user, session_scope):
	user = make_user()
	client = client_for(user)
	client.post("/inventory/create", data=FORM)
	dev = device_by_label(session_scope, "edge-1")
	assert dev and dev.user_id == user.id and dev.is_global is False

	client.post(f"/inventory/{dev.id}/edit", data={
		**FORM, "label": "edge-1b", "attr_hostname": "e1",
		"attr_vrfs": "red, blue"})
	edited = device_by_label(session_scope, "edge-1b")
	assert edited.var_maps == {"hostname": "e1", "vrfs": ["red", "blue"]}

	client.post(f"/inventory/{dev.id}/delete")
	assert device_by_label(session_scope, "edge-1b") is None


def test_cannot_touch_another_users_device(client_for, make_user, make_device,
                                           db_get):
	owner, other = make_user(), make_user()
	dev = make_device(owner)
	client = client_for(other)
	client.post(f"/inventory/{dev}/edit", data={**FORM, "label": "hijacked"})
	client.post(f"/inventory/{dev}/delete")
	assert db_get(Inventory, dev).label == "dev-10.0.0.1"


def test_invalid_profile_id_is_rejected_without_partial_edit(
		client_for, make_user, make_device, db_get):
	user = make_user()
	dev = make_device(user)
	resp = client_for(user).post(f"/inventory/{dev}/edit", data={
		**FORM, "label": "changed", "sec_profile_id": "not-a-uuid"})
	assert resp.status_code == 422
	assert db_get(Inventory, dev).label == "dev-10.0.0.1"  # nothing applied


def test_connection_test_endpoint(client_for, make_user):
	client = client_for(make_user())
	with patch("src.validation.Validator.test_tcp_port", return_value=True):
		ok = client.post("/inventory/test_connection",
		                 json={"ip": "10.0.0.1", "port": "22"})
	assert ok.json["status"] == "ok"
	bad = client.post("/inventory/test_connection",
	                  json={"ip": "not-an-ip", "port": "22"})
	assert bad.status_code == 400


def test_csv_import(client_for, make_user, session_scope):
	user = make_user()
	csv = ("ip,username,password,device_type,secret,port\n"
	       "10.2.2.1,u,p,cisco_ios,s,22\n"
	       "not-an-ip,u,p,cisco_ios,s,22\n")
	with patch("src.validation.Validator.test_tcp_port", return_value=True):
		client_for(user).post("/inventory/import_csv", data={
			"csv_file": (io.BytesIO(csv.encode()), "devices.csv")},
			content_type="multipart/form-data")
	with session_scope() as s:
		ips = [d.ip for d in s.query(Inventory).filter_by(user_id=user.id)]
	assert ips == ["10.2.2.1"]


def test_csv_import_tolerates_blanks_and_needs_no_credentials(
		client_for, make_user, session_scope):
	# credentials aren't stored in inventory, so the columns are optional;
	# a blank label falls back to the IP; a bad row doesn't sink the rest
	user = make_user()
	csv = ("ip,device_type,port,label\n"
	       "10.3.3.1,cisco_ios,22,\n"
	       "10.3.3.2,not_a_platform,22,x\n"
	       "10.3.3.3,arista_eos,22,edge-3\n")
	client = client_for(user)
	with patch("src.validation.Validator.test_tcp_port", return_value=True):
		client.post("/inventory/import_csv", data={
			"csv_file": (io.BytesIO(csv.encode()), "devices.csv")},
			content_type="multipart/form-data")
	with session_scope() as s:
		rows = sorted((d.ip, d.label) for d in
		              s.query(Inventory).filter_by(user_id=user.id))
	# row label used; blank label falls back to the IP
	assert rows == [("10.3.3.1", "10.3.3.1"), ("10.3.3.3", "edge-3")]
	assert any("Row 2" in m for m in flashes(client))


def test_json_routes_report_invalid_request_plainly(client_for, make_user):
	resp = client_for(make_user()).post("/inventory/bulk_assign", data="x",
	                                    content_type="application/json")
	assert resp.json["message"] == "Invalid request"


def test_unresolvable_mappings_are_not_bound(client_for, make_user,
                                             make_device, make_mapping,
                                             session_scope):
	user = make_user()
	dev = make_device(user, var_maps={"vrfs": ["red"]})
	host = make_mapping(user, token="HOST", prop="hostname")   # attr missing
	vrf2 = make_mapping(user, token="VRF2", prop="vrfs", index=2)  # out of range
	vrf0 = make_mapping(user, token="VRF0", prop="vrfs", index=0)  # fine
	client = client_for(user)
	client.post(f"/inventory/{dev}/mappings", data={
		"mapping_ids": [str(host), str(vrf2), str(vrf0)]})
	with session_scope() as s:
		bound = {m.token for m in s.get(Inventory, dev).var_mappings}
	assert bound == {"$$VRF0$$"}
	assert any("$$HOST$$" in m and "$$VRF2$$" in m for m in flashes(client))
	# drag-assign applies the same rule
	client.post("/mappings/bulk_assign", json={
		"mapping_id": str(vrf2), "device_ids": [str(dev)]})
	with session_scope() as s:
		bound = {m.token for m in s.get(Inventory, dev).var_mappings}
	assert "$$VRF2$$" not in bound


def test_bulk_profile_assign_only_own_profile_and_devices(
		client_for, make_user, make_profile, make_device, db_get):
	user, other = make_user(), make_user()
	mine, theirs = make_profile(user), make_profile(other)
	dev = make_device(user)
	client = client_for(user)
	resp = client.post("/inventory/bulk_assign",
	                   json={"profile_id": str(theirs), "device_ids": [str(dev)]})
	assert resp.status_code == 404
	resp = client.post("/inventory/bulk_assign",
	                   json={"profile_id": str(mine), "device_ids": [str(dev)]})
	assert resp.json["status"] == "ok"
	assert db_get(Inventory, dev).sec_profile_id == mine


# ── Global devices ───────────────────────────────────────────────────────────

@pytest.fixture
def world(make_user, make_profile, make_device, make_mapping):
	admin = make_user(role="admin")
	user_b, user_c = make_user(), make_user()
	admin_prof = make_profile(admin, label="core-ro")
	b_prof = make_profile(user_b, label="b-prof")
	core = make_device(admin, ip="10.50.0.1", label="CORE-X", is_global=True,
	                   profile_id=admin_prof, var_maps={"hostname": "core-x"})
	b_local = make_device(user_b, ip="10.50.0.1", label="B-LOCAL",
	                      profile_id=b_prof, var_maps={"hostname": "bl"})
	map_b = make_mapping(user_b)
	map_c = make_mapping(user_c, devices=[core])
	return type("World", (), dict(
		admin=admin, b=user_b, c=user_c, admin_prof=admin_prof,
		core=core, b_local=b_local, map_b=map_b, map_c=map_c))


def test_inventory_splits_sections_only_when_both_exist(world, client_for):
	html_b = client_for(world.b).get("/inventory").get_data(as_text=True)
	assert "Global Devices" in html_b and "My Devices" in html_b
	html_c = client_for(world.c).get("/inventory").get_data(as_text=True)
	assert "CORE-X" in html_c and 'class="nr-section-head' not in html_c


def test_global_card_is_read_only_for_users(world, client_for):
	html = client_for(world.b).get("/inventory").get_data(as_text=True)
	card = re.search(r'<div class="inv-card is-global".*?</div>\s*</div>',
	                 html, re.S).group(0)
	assert "card-delete-btn" not in card and '"editable": false' in card
	assert 'id="addIsGlobal"' not in html  # toggle is admin-only


def test_admin_profile_id_never_exposed_to_users(world, client_for):
	html = client_for(world.b).get("/inventory").get_data(as_text=True)
	assert str(world.admin_prof) not in html


def test_users_cannot_edit_delete_or_hijack_credentials(world, client_for,
                                                        db_get, session_scope):
	client = client_for(world.b)
	client.post(f"/inventory/{world.core}/edit", data={**FORM, "label": "X"})
	client.post(f"/inventory/{world.core}/delete")
	assert db_get(Inventory, world.core).label == "CORE-X"
	# attaching the admin's profile to a device the user controls
	client.post("/inventory/create", data={
		**FORM, "label": "hijack", "sec_profile_id": str(world.admin_prof)})
	assert device_by_label(session_scope, "hijack") is None
	client.post(f"/inventory/{world.b_local}/edit", data={
		**FORM, "label": "B-LOCAL", "sec_profile_id": str(world.admin_prof)})
	assert db_get(Inventory, world.b_local).sec_profile_id != world.admin_prof


def test_users_cannot_create_global_devices(world, client_for, session_scope):
	client_for(world.b).post("/inventory/create", data={
		**FORM, "label": "sneaky", "is_global": "on"})
	assert device_by_label(session_scope, "sneaky").is_global is False


def test_users_bind_own_mappings_without_touching_others(world, client_for,
                                                         session_scope):
	client = client_for(world.b)
	client.post(f"/inventory/{world.core}/mappings",
	            data={"mapping_ids": [str(world.map_b)]})
	assert mapping_owners(session_scope, world.core) == \
	       sorted([str(world.b.id), str(world.c.id)])
	# another user's mapping can't be bound; own binding replaced
	client.post(f"/inventory/{world.core}/mappings",
	            data={"mapping_ids": [str(world.map_c)]})
	assert mapping_owners(session_scope, world.core) == [str(world.c.id)]
	# drag-assign path
	client.post("/mappings/bulk_assign", json={
		"mapping_id": str(world.map_b), "device_ids": [str(world.core)]})
	assert mapping_owners(session_scope, world.core) == \
	       sorted([str(world.b.id), str(world.c.id)])


def test_admin_must_attach_profile_to_global_device(world, client_for,
                                                   session_scope):
	client_for(world.admin).post("/inventory/create", data={
		**FORM, "label": "g-noprof", "is_global": "on"})
	assert device_by_label(session_scope, "g-noprof") is None


def test_localizing_drops_foreign_bindings_and_audits(world, client_for,
                                                      db_get, session_scope):
	client_for(world.admin).post(f"/inventory/{world.core}/edit", data={
		**FORM, "label": "CORE-X", "ip": "10.50.0.1",
		"sec_profile_id": str(world.admin_prof), "attr_hostname": "core-x"})
	assert db_get(Inventory, world.core).is_global is False
	assert mapping_owners(session_scope, world.core) == []
	with session_scope() as s:
		actions = {a.action for a in s.query(AuditLog).filter(
			AuditLog.object_id == world.core)}
	assert "inventory.localize" in actions
	html = client_for(world.b).get("/inventory").get_data(as_text=True)
	assert str(world.core) not in html


def test_deleting_a_mapping_keeps_its_devices(world, client_for, db_get):
	client_for(world.c).post(f"/mappings/{world.map_c}/delete")
	assert db_get(Inventory, world.core) is not None  # was cascade-deleted


def test_global_devices_offered_in_rollout_and_mappings(world, client_for):
	b = client_for(world.b)
	rollout = b.get("/rollout/new").get_data(as_text=True)
	assert "Global Devices" in rollout and str(world.core) in rollout
	mappings = b.get("/mappings").get_data(as_text=True)
	assert '"is_global": true' in mappings


# ── Reachability ─────────────────────────────────────────────────────────────

def test_reachability_endpoint_reports_visible_devices_only(
		client_for, make_user, make_device, unreachable_targets):
	user, other = make_user(), make_user()
	up = make_device(user, ip="10.8.0.1")
	down = make_device(user, ip="10.8.0.2")
	foreign = make_device(other, ip="10.8.0.3")
	unreachable_targets.add(("10.8.0.2", 22))
	resp = client_for(user).post("/inventory/reachability", json={
		"device_ids": [str(up), str(down), str(foreign)]})
	statuses = resp.json["statuses"]
	assert set(statuses) == {str(up), str(down)}  # other user's device omitted
	assert statuses[str(up)]["reachable"] is True
	assert statuses[str(down)]["reachable"] is False
	assert client_for(user).post("/inventory/reachability", json={
		"device_ids": ["nope"]}).status_code == 422


def test_inventory_cards_have_reachability_indicator(client_for, make_user,
                                                     make_device):
	user = make_user()
	dev = make_device(user)
	html = client_for(user).get("/inventory").get_data(as_text=True)
	assert f'data-reach-for="{dev}"' in html and 'id="reachRecheck"' in html
