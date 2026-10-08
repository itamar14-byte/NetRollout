"""Inventory CRUD, CSV import, bulk profile assign, and global devices
(visibility, admin-only edits, per-user mappings, credential isolation)."""
import io
import re
from unittest.mock import patch

import pytest

from src.db.tables import AuditLog, DeviceAttribute, Inventory, SecurityProfile
from src.encryption import decrypt

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

FORM = {"label": "edge-1", "ip": "10.1.1.1", "port": "22",
        "device_type": "cisco_ios"}


def flashes(client):
	"""The flash messages waiting in the client's session."""
	with client.session_transaction() as s:
		return [m for _, m in s.get("_flashes", [])]


def device_by_label(session_scope, label):
	"""The inventory device with `label` (detached), or None."""
	with session_scope() as s:
		d = s.query(Inventory).filter_by(label=label).first()
		if d:
			s.expunge(d)
		return d


def mapping_owners(session_scope, device_id):
	"""The user ids owning the mappings bound to a device, sorted."""
	with session_scope() as s:
		return sorted(str(m.user_id) for m in s.get(Inventory, device_id).var_mappings)


# ── CRUD ─────────────────────────────────────────────────────────────────────

def test_create_edit_delete_own_device(client_for, make_user, session_scope):
	"""A user creates a private device, edits it (attributes saved as var_maps, a list
	split on commas) and deletes it."""
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


BAD_FIELDS = [
	({"ip": "300.1.1.1"}, "Not a valid IP address: 300.1.1.1."),
	({"port": "abc"}, "The port is a number from 1 to 65535."),
	({"port": "70000"}, "The port is a number from 1 to 65535."),
	({"device_type": "foo_os"}, "Unsupported device type: foo_os."),
]


@pytest.mark.parametrize("bad, message", BAD_FIELDS)
def test_create_refuses_a_bad_field_in_words(client_for, make_user, session_scope,
                                             bad, message):
	"""Add device checks the IP, port and device type on the server: a bad one
	is refused with the reason (cases: an IP out of range, a port that isn't
	a number or is too high, an unsupported type) and nothing is saved."""
	client = client_for(make_user())
	resp = client.post("/inventory/create", data={**FORM, **bad})
	assert resp.headers["Location"] == "/inventory"
	assert flashes(client) == [message]
	assert device_by_label(session_scope, "edge-1") is None


@pytest.mark.parametrize("bad, message", BAD_FIELDS)
def test_edit_refuses_a_bad_field_in_words(client_for, make_user, session_scope,
                                           bad, message):
	"""Edit device checks the same fields: a bad one is refused with the
	reason and the device stays as it was."""
	client = client_for(make_user())
	client.post("/inventory/create", data=FORM)
	dev = device_by_label(session_scope, "edge-1")
	with client.session_transaction() as s:     # the create's confirmation
		s.pop("_flashes", None)
	resp = client.post(f"/inventory/{dev.id}/edit", data={**FORM, **bad})
	assert resp.headers["Location"] == "/inventory"
	assert flashes(client) == [message]
	same = device_by_label(session_scope, "edge-1")
	assert (same.ip, same.port, same.device_type) == ("10.1.1.1", 22, "cisco_ios")


def test_an_ipv6_device_is_added_and_its_endpoint_written_in_brackets(
		client_for, make_user, make_device, session_scope):
	"""Add device accepts an IPv6 address, and the warning about a shared
	endpoint writes it as [address]:port."""
	user = make_user()
	make_device(user, ip="2001:db8::5", label="core-v6")
	client = client_for(user)
	client.post("/inventory/create", data={**FORM, "label": "edge-v6",
	                                       "ip": "2001:db8::5"})
	assert device_by_label(session_scope, "edge-v6").ip == "2001:db8::5"
	(msg,) = dup_warnings(client)
	assert msg.startswith("[2001:db8::5]:22 is already used by core-v6.")


def test_an_ipv6_address_is_stored_in_its_standard_form(client_for, make_user,
                                                        session_scope):
	"""Add and Edit store an IPv6 address in its one standard spelling, so the
	same address typed another way is recognised as the same endpoint (the
	duplicate warning)."""
	client = client_for(make_user())
	client.post("/inventory/create", data={**FORM, "label": "v6-a",
	                                       "ip": "2001:DB8:0:0:0:0:0:1"})
	assert device_by_label(session_scope, "v6-a").ip == "2001:db8::1"
	with client.session_transaction() as s:
		s.pop("_flashes", None)
	client.post("/inventory/create", data={**FORM, "label": "v6-b",
	                                       "ip": "2001:0db8::0001"})
	assert device_by_label(session_scope, "v6-b").ip == "2001:db8::1"
	(msg,) = dup_warnings(client)
	assert msg.startswith("[2001:db8::1]:22 is already used by v6-a.")
	dev = device_by_label(session_scope, "v6-b")
	client.post(f"/inventory/{dev.id}/edit", data={**FORM, "label": "v6-b",
	                                               "ip": "2001:DB8::2"})
	assert device_by_label(session_scope, "v6-b").ip == "2001:db8::2"


def test_a_device_without_a_label_is_named_by_its_ip(client_for, make_user,
                                                     session_scope):
	"""A device added without a label gets its IP as the label (as the CSV
	import does), and the confirmation names it."""
	client = client_for(make_user())
	client.post("/inventory/create", data={**FORM, "label": "  "})
	assert device_by_label(session_scope, "10.1.1.1") is not None
	assert flashes(client) == ["10.1.1.1 added to inventory."]


def test_cannot_touch_another_users_device(client_for, make_user, make_device,
                                           db_get):
	"""Another user's edit and delete of a device change nothing."""
	owner, other = make_user(), make_user()
	dev = make_device(owner)
	client = client_for(other)
	client.post(f"/inventory/{dev}/edit", data={**FORM, "label": "hijacked"})
	client.post(f"/inventory/{dev}/delete")
	assert db_get(Inventory, dev).label == "dev-10.0.0.1"


def test_invalid_profile_id_is_rejected_without_partial_edit(
		client_for, make_user, make_device, db_get):
	"""An edit with a malformed profile id is 422 and applies none of the edit."""
	user = make_user()
	dev = make_device(user)
	resp = client_for(user).post(f"/inventory/{dev}/edit", data={
		**FORM, "label": "changed", "sec_profile_id": "not-a-uuid"})
	assert resp.status_code == 422
	assert db_get(Inventory, dev).label == "dev-10.0.0.1"  # nothing applied


def test_connection_test_endpoint(client_for, make_user):
	"""The connection test answers ok for a reachable device and 400 for an invalid IP."""
	client = client_for(make_user())
	with patch("src.rollout.inputs.tcp_reachable", return_value=True):
		ok = client.post("/inventory/test_connection",
		                 json={"ip": "10.0.0.1", "port": "22"})
	assert ok.json["status"] == "ok"
	bad = client.post("/inventory/test_connection",
	                  json={"ip": "not-an-ip", "port": "22"})
	assert bad.status_code == 400


def test_csv_import(client_for, make_user, session_scope):
	"""A CSV import saves the valid row and skips the one with an invalid IP."""
	user = make_user()
	csv = ("ip,username,password,device_type,secret,port\n"
	       "10.2.2.1,u,p,cisco_ios,s,22\n"
	       "not-an-ip,u,p,cisco_ios,s,22\n")
	with patch("src.rollout.inputs.tcp_reachable", return_value=True):
		client_for(user).post("/inventory/import_csv", data={
			"csv_file": (io.BytesIO(csv.encode()), "devices.csv")},
			content_type="multipart/form-data")
	with session_scope() as s:
		ips = [d.ip for d in s.query(Inventory).filter_by(user_id=user.id)]
	assert ips == ["10.2.2.1"]


def test_csv_import_tolerates_blanks_and_needs_no_credentials(
		client_for, make_user, session_scope):
	"""Credential columns are optional in an import (they aren't stored in inventory);
	a blank label falls back to the IP; a bad row is reported and doesn't sink the rest."""
	user = make_user()
	csv = ("ip,device_type,port,label\n"
	       "10.3.3.1,cisco_ios,22,\n"
	       "10.3.3.2,not_a_platform,22,x\n"
	       "10.3.3.3,arista_eos,22,edge-3\n")
	client = client_for(user)
	client.post("/inventory/import_csv", data={
		"csv_file": (io.BytesIO(csv.encode()), "devices.csv")},
		content_type="multipart/form-data")
	with session_scope() as s:
		rows = sorted((d.ip, d.label) for d in
		              s.query(Inventory).filter_by(user_id=user.id))
	# row label used; blank label falls back to the IP
	assert rows == [("10.3.3.1", "10.3.3.1"), ("10.3.3.3", "edge-3")]
	assert any("Row 2" in m for m in flashes(client))


# ── CSV import: one format for the CLI and the web app ──────────────────────

def import_csv(client, csv, create_profiles=True, label=None):
	"""Posts `csv` to the CSV import, profile creation on unless told otherwise."""
	data = {"csv_file": (io.BytesIO(csv.encode()), "devices.csv")}
	if create_profiles:
		data["create_profiles"] = "on"
	if label:
		data["label"] = label
	return client.post("/inventory/import_csv", data=data,
	                   content_type="multipart/form-data")


def devices_of(session_scope, user):
	"""The user's devices by label (detached)."""
	with session_scope() as s:
		rows = {d.label: d for d in s.query(Inventory).filter_by(user_id=user.id)}
		s.expunge_all()
		return rows


def profiles_of(session_scope, user):
	"""The user's security profiles by id (detached)."""
	with session_scope() as s:
		rows = {p.id: p for p in
		        s.query(SecurityProfile).filter_by(user_id=user.id)}
		s.expunge_all()
		return rows


def test_csv_import_saves_attribute_columns(client_for, make_user,
                                            session_scope):
	"""Attribute columns, matched by property name or label in any case, are saved
	(list properties split) - system ones as the device's var_maps, custom ones as
	the importing user's own values; empty cells are skipped; an unknown column is
	reported as ignored."""
	user = make_user()
	client = client_for(user)
	client.post("/properties/create", json={"name": "rack", "label": "Rack"})
	client.post("/properties/create", json={"name": "uplinks",
	                                        "label": "Uplinks", "is_list": True})
	# headers by name, by label ("Loopback IP"), any case; one unknown column
	csv = ("ip,device_type,port,label,hostname,Loopback IP,vrfs,RACK,Uplinks,rack_no\n"
	       "10.4.4.1,cisco_ios,22,r1,r1.lab,1.1.1.1,\"red, blue\",R12,\"Gi0/1,Gi0/2\",7\n"
	       "10.4.4.2,cisco_ios,22,r2,,,,,,\n")
	import_csv(client, csv)
	devs = devices_of(session_scope, user)
	assert devs["r1"].var_maps == {
		"hostname": "r1.lab", "loopback_ip": "1.1.1.1", "vrfs": ["red", "blue"]}
	with session_scope() as s:
		custom = {(a.device_id, a.user_id, a.name): a.value
		          for a in s.query(DeviceAttribute)}
	assert custom == {(devs["r1"].id, user.id, "rack"): "R12",
	                  (devs["r1"].id, user.id, "uplinks"): ["Gi0/1", "Gi0/2"]}
	assert devs["r2"].var_maps is None  # empty cells are skipped
	msgs = flashes(client)
	assert any(m.startswith("Ignored columns: rack_no") for m in msgs)


def test_csv_import_turns_credentials_into_profiles(client_for, make_user,
                                                    make_profile, session_scope):
	"""Import credentials become profiles: an exact match reuses the existing one, new
	credentials make one new profile shared by their rows (audited like a manual one),
	no credentials no profile; the flashes count each."""
	user = make_user()
	core = make_profile(user, label="core-ro", username="ro", password="ropw")
	csv = ("ip,device_type,port,label,username,password,secret\n"
	       "10.5.5.1,cisco_ios,22,a,ro,ropw,\n"        # exact match → reused
	       "10.5.5.2,cisco_ios,22,b,admin,admin,en\n"  # new profile …
	       "10.5.5.3,cisco_ios,22,c,admin,admin,en\n"  # … shared by this row
	       "10.5.5.4,cisco_ios,22,d,,,\n")             # no credentials
	client = client_for(user)
	import_csv(client, csv)
	devs = devices_of(session_scope, user)
	profiles = profiles_of(session_scope, user)
	assert devs["a"].sec_profile_id == core
	assert devs["b"].sec_profile_id == devs["c"].sec_profile_id != core
	assert devs["d"].sec_profile_id is None
	new = profiles[devs["b"].sec_profile_id]
	assert new.label.startswith("admin · CSV import ")
	assert (new.username, decrypt(new.password_secret),
	        decrypt(new.enable_secret)) == ("admin", "admin", "en")
	assert len(profiles) == 2
	# the existing profile is untouched
	assert decrypt(profiles[core].password_secret) == "ropw"
	msgs = flashes(client)
	assert f"2 devices assigned to new profile '{new.label}'" in msgs
	assert "1 device assigned to existing profile 'core-ro'" in msgs
	# audited like a manually created profile
	with session_scope() as s:
		audit = s.query(AuditLog).filter_by(action="security_profile.create",
		                                    object_id=new.id).one()
		assert audit.detail == {"source": "csv_import"}
		assert audit.object_label == new.label


def test_csv_import_warns_on_same_username_other_password(
		client_for, make_user, make_profile, session_scope):
	"""Same username with another password makes a new profile with a warning naming the
	existing one (never changed); a second import that day gets a distinct label."""
	user = make_user()
	core = make_profile(user, label="core", username="admin", password="admin")
	csv = ("ip,device_type,port,label,username,password\n"
	       "10.6.6.1,cisco_ios,22,a,admin,admin\n"     # reuses core
	       "10.6.6.2,cisco_ios,22,b,admin,admin1\n")   # typo → new + warning
	client = client_for(user)
	import_csv(client, csv)
	devs = devices_of(session_scope, user)
	profiles = profiles_of(session_scope, user)
	assert devs["a"].sec_profile_id == core
	new = profiles[devs["b"].sec_profile_id]
	assert decrypt(profiles[core].password_secret) == "admin"  # never changed
	assert any(m.startswith(f"Created profile '{new.label}' — your profile "
	                        f"'core' also uses username admin, with a "
	                        f"different password.") for m in flashes(client))
	# a second import the same day gets a distinct label
	import_csv(client, "ip,device_type,port,label,username,password\n"
	                   "10.6.6.3,cisco_ios,22,c,admin,admin2\n")
	third = profiles_of(session_scope, user)[
		devices_of(session_scope, user)["c"].sec_profile_id]
	assert third.label == new.label + " (2)"


def test_csv_import_without_profile_creation(client_for, make_user,
                                             session_scope):
	"""With profile creation off, credentials are not imported (no profile) and a flash
	says so."""
	user = make_user()
	client = client_for(user)
	import_csv(client, "ip,device_type,port,label,username,password\n"
	                   "10.7.7.1,cisco_ios,22,a,admin,admin\n",
	           create_profiles=False)
	assert devices_of(session_scope, user)["a"].sec_profile_id is None
	assert profiles_of(session_scope, user) == {}
	assert any("Credential columns were not imported" in m
	           for m in flashes(client))


def test_csv_import_partial_credentials_get_no_profile(client_for, make_user,
                                                       session_scope):
	"""A row with a username but no password is imported without a profile, named in a
	flash."""
	user = make_user()
	client = client_for(user)
	import_csv(client, "ip,device_type,port,label,username,password\n"
	                   "10.8.8.1,cisco_ios,22,a,admin,\n")
	assert devices_of(session_scope, user)["a"].sec_profile_id is None
	assert profiles_of(session_scope, user) == {}
	assert "1 device imported without a profile (username and password are " \
	       "both needed): a" in flashes(client)


def test_csv_import_does_not_check_reachability(client_for, make_user,
                                                session_scope):
	"""The import saves an unreachable device and never probes it."""
	user = make_user()
	with patch("src.rollout.inputs.tcp_reachable",
	           return_value=False) as probe:
		import_csv(client_for(user), "ip,device_type,port,label\n"
		                             "10.9.9.1,cisco_ios,22,offline\n")
	assert "offline" in devices_of(session_scope, user)
	probe.assert_not_called()


def test_csv_import_missing_required_columns(client_for, make_user):
	"""An import without a required column is refused, naming the missing column."""
	client = client_for(make_user())
	import_csv(client, "ip,device_type\n10.1.1.1,cisco_ios\n")
	assert "Missing required columns: port" in flashes(client)


# ── Same ip:port as another device: warn, never block ───────────────────────

def dup_warnings(client):
	"""The flashes warning about a shared ip:port."""
	return [m for m in flashes(client) if "is already used by" in m
	        or "share an ip:port" in m]


def test_create_warns_on_same_endpoint_as_own_device(client_for, make_user,
                                                     make_device,
                                                     session_scope):
	"""Creating a device on an ip:port the user already has saves it with a warning
	naming the other device; the same IP on another port gets no warning."""
	user = make_user()
	make_device(user, ip="10.1.1.1", label="core-a")
	client = client_for(user)
	client.post("/inventory/create", data=FORM)          # 10.1.1.1:22
	assert device_by_label(session_scope, "edge-1")     # saved anyway
	(msg,) = dup_warnings(client)
	assert msg.startswith("10.1.1.1:22 is already used by core-a.")
	# another port on the same IP (port forwarding) is a different endpoint
	other = client_for(user)
	other.post("/inventory/create", data={**FORM, "label": "edge-2",
	                                      "port": "2222"})
	assert dup_warnings(other) == []


def test_create_warns_on_global_device_but_never_on_private_ones(
		client_for, make_user, make_device):
	"""A clash with a global device is warned about (marked global); another user's
	private device is never mentioned."""
	admin, user, stranger = (make_user(role="admin"), make_user(),
	                         make_user())
	make_device(admin, ip="10.1.1.1", label="core-g", is_global=True)
	make_device(stranger, ip="10.9.9.9", label="theirs")   # invisible to user
	client = client_for(user)
	client.post("/inventory/create", data=FORM)
	assert dup_warnings(client) == [
		"10.1.1.1:22 is already used by core-g (global). That's fine for NAT, "
		"VRFs or port-forwarded labs, but they can't be in the same rollout."]
	private = client_for(user)
	private.post("/inventory/create", data={**FORM, "label": "x",
	                                        "ip": "10.9.9.9"})
	assert dup_warnings(private) == []   # other users' devices never leak


def test_edit_warns_only_when_the_endpoint_changes(client_for, make_user,
                                                   make_device):
	"""An edit warns about a shared ip:port only when it changes the endpoint."""
	user = make_user()
	make_device(user, ip="10.1.1.1", label="core-a")
	dev = make_device(user, ip="10.2.2.2", label="edge")
	changed = client_for(user)
	changed.post(f"/inventory/{dev}/edit", data={**FORM, "label": "edge",
	                                             "ip": "10.1.1.1"})
	assert len(dup_warnings(changed)) == 1
	unchanged = client_for(user)     # saving again, endpoint unchanged
	unchanged.post(f"/inventory/{dev}/edit", data={**FORM, "label": "edge2",
	                                               "ip": "10.1.1.1"})
	assert dup_warnings(unchanged) == []


def test_csv_import_warns_on_shared_endpoints(client_for, make_user,
                                              make_device, session_scope):
	"""An import saves every row and warns once, listing the endpoints shared with an
	existing device or within the file."""
	user = make_user()
	make_device(user, ip="10.1.1.1", label="core-a")
	client = client_for(user)
	import_csv(client, "ip,device_type,port,label\n"
	                   "10.1.1.1,cisco_ios,22,dup-existing\n"
	                   "10.3.3.3,cisco_ios,22,twin-1\n"
	                   "10.3.3.3,cisco_ios,22,twin-2\n"
	                   "10.4.4.4,cisco_ios,22,alone\n")
	assert len(devices_of(session_scope, user)) == 5     # all imported
	(msg,) = dup_warnings(client)
	assert msg.startswith("2 imported devices share an ip:port with another "
	                      "device: 10.1.1.1:22, 10.3.3.3:22.")


def test_json_routes_report_invalid_request_plainly(client_for, make_user):
	"""A malformed JSON body gets the plain message "Invalid request"."""
	resp = client_for(make_user()).post("/inventory/bulk_assign", data="x",
	                                    content_type="application/json")
	assert resp.json["message"] == "Invalid request"


def test_unresolvable_mappings_are_not_bound(client_for, make_user,
                                             make_device, make_mapping,
                                             session_scope):
	"""Mappings the device can't resolve (attribute missing, list index out of range)
	are not bound and are named in a flash; drag-assign applies the same rule."""
	user = make_user()
	dev = make_device(user, var_maps={"vrfs": ["red"]})
	host = make_mapping(user, token="HOST", prop="hostname")   # attr missing
	vrf2 = make_mapping(user, token="VRF2", prop="vrfs", index=2)  # out of range
	vrf0 = make_mapping(user, token="VRF0", prop="vrfs", index=0)  # fine
	client = client_for(user)
	client.post(f"/inventory/{dev}/attributes", data={
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
	"""Bulk assign of another user's profile is 404; the user's own profile is assigned."""
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


def test_bulk_unassign_keeps_global_devices_profile(
		client_for, make_user, make_profile, make_device, db_get):
	"""Bulk unassign clears a local device's profile but a global device keeps its own
	(the same rule as create/edit: a global device must keep a profile)."""
	admin = make_user(role="admin")
	prof = make_profile(admin)
	glob = make_device(admin, ip="10.0.0.1", profile_id=prof, is_global=True)
	local = make_device(admin, ip="10.0.0.2", profile_id=prof)
	client_for(admin).post("/inventory/bulk_assign", json={
		"profile_id": None, "device_ids": [str(glob), str(local)]})
	assert db_get(Inventory, glob).sec_profile_id == prof
	assert db_get(Inventory, local).sec_profile_id is None


# ── Global devices ───────────────────────────────────────────────────────────

@pytest.fixture
def world(make_user, make_profile, make_device, make_mapping):
	"""An admin's global device with a profile, user b's local device on the same IP,
	and a mapping each for b and c (c's bound to the global device)."""
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


def rendered(client, path):
	"""Page markup without <script> blocks: shared JS (e.g. the assign
	board) contains section-header strings of its own."""
	html = client.get(path).get_data(as_text=True)
	return re.sub(r"<script\b.*?</script>", "", html, flags=re.S)


def test_inventory_splits_sections_only_when_both_exist(world, client_for):
	"""The inventory splits into Global Devices and My Devices only for a user who has
	both; a user with global devices only gets no section headers."""
	html_b = rendered(client_for(world.b), "/inventory")
	assert "Global Devices" in html_b and "My Devices" in html_b
	html_c = rendered(client_for(world.c), "/inventory")
	assert "CORE-X" in html_c and 'class="nr-section-head' not in html_c


def test_global_card_is_read_only_for_users(world, client_for):
	"""A user's global device card has no delete button and isn't editable, and the
	global toggle is admin-only."""
	html = client_for(world.b).get("/inventory").get_data(as_text=True)
	card = re.search(r'<div class="inv-card is-global".*?</div>\s*</div>',
	                 html, re.S).group(0)
	assert "card-delete-btn" not in card and '"editable": false' in card
	assert 'id="addIsGlobal"' not in html  # toggle is admin-only


def test_admin_profile_id_never_exposed_to_users(world, client_for):
	"""The admin profile's id of a global device never appears in a user's inventory
	page."""
	html = client_for(world.b).get("/inventory").get_data(as_text=True)
	assert str(world.admin_prof) not in html


def test_users_cannot_edit_delete_or_hijack_credentials(world, client_for,
                                                        db_get, session_scope):
	"""A user can't edit or delete a global device, nor attach the admin's profile to a
	new or own device."""
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
	"""A user's create with is_global on makes a private device."""
	client_for(world.b).post("/inventory/create", data={
		**FORM, "label": "sneaky", "is_global": "on"})
	assert device_by_label(session_scope, "sneaky").is_global is False


def test_users_bind_own_mappings_without_touching_others(world, client_for,
                                                         session_scope):
	"""A user binds own mappings to a global device beside another user's; another
	user's mapping can't be bound (the own binding is replaced); drag-assign too."""
	client = client_for(world.b)
	client.post(f"/inventory/{world.core}/attributes",
	            data={"mapping_ids": [str(world.map_b)]})
	assert mapping_owners(session_scope, world.core) == \
	       sorted([str(world.b.id), str(world.c.id)])
	# another user's mapping can't be bound; own binding replaced
	client.post(f"/inventory/{world.core}/attributes",
	            data={"mapping_ids": [str(world.map_c)]})
	assert mapping_owners(session_scope, world.core) == [str(world.c.id)]
	# drag-assign path
	client.post("/mappings/bulk_assign", json={
		"mapping_id": str(world.map_b), "device_ids": [str(world.core)]})
	assert mapping_owners(session_scope, world.core) == \
	       sorted([str(world.b.id), str(world.c.id)])


def test_admin_must_attach_profile_to_global_device(world, client_for,
                                                   session_scope):
	"""An admin's global device without a profile is not created."""
	client_for(world.admin).post("/inventory/create", data={
		**FORM, "label": "g-noprof", "is_global": "on"})
	assert device_by_label(session_scope, "g-noprof") is None


def test_localizing_drops_foreign_bindings_and_audits(world, client_for,
                                                      db_get, session_scope):
	"""Making a global device local drops other users' mapping bindings, is audited
	(`inventory.localize`) and hides the device from other users."""
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
	"""Deleting a mapping keeps the devices bound to it (they used to be
	cascade-deleted)."""
	client_for(world.c).post(f"/mappings/{world.map_c}/delete")
	assert db_get(Inventory, world.core) is not None  # was cascade-deleted


def test_global_devices_offered_in_rollout_and_mappings(world, client_for):
	"""Global devices are offered to users on the new rollout and mappings pages."""
	b = client_for(world.b)
	rollout = b.get("/rollout/new").get_data(as_text=True)
	assert "Global Devices" in rollout and str(world.core) in rollout
	mappings = b.get("/mappings").get_data(as_text=True)
	assert '"is_global": true' in mappings


# ── Reachability ─────────────────────────────────────────────────────────────

def test_reachability_endpoint_reports_visible_devices_only(
		client_for, make_user, make_device, unreachable_targets):
	"""The reachability endpoint reports up/down for the user's own devices, omits another
	user's, and answers 422 for an invalid id."""
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
	"""Each inventory card has a reachability indicator and the page a recheck button."""
	user = make_user()
	dev = make_device(user)
	html = client_for(user).get("/inventory").get_data(as_text=True)
	assert f'data-reach-for="{dev}"' in html and 'id="reachRecheck"' in html
