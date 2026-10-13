"""Personal credentials: a device's security profile is each user's own
(device_credentials) - the database rule, rollouts and rollbacks with each
user's credentials, the Inventory's ACTION NEEDED, global <-> private, and
deleting the people they belong to."""
import io
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from src.db.tables import AuditLog, DeviceCredential, Inventory, User
from tests.integration.test_rollouts_and_jobs import add_result

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

FORM = {"label": "d", "ip": "10.70.0.9", "port": "22", "device_type": "cisco_ios"}


@pytest.fixture
def shared(make_user, make_profile, make_device):
	"""An admin's global device CORE (linked to the admin's profile), and two
	operators with a profile each, neither linked to CORE."""
	admin, a, b = make_user(role="admin"), make_user(), make_user()
	admin_prof = make_profile(admin, label="admin-ro", username="admin-user")
	core = make_device(admin, ip="10.70.0.1", label="CORE", is_global=True,
	                   profile_id=admin_prof)
	a_prof = make_profile(a, label="a-prof", username="a-user")
	b_prof = make_profile(b, label="b-prof", username="b-user")
	return type("Shared", (), dict(admin=admin, a=a, b=b, core=core, admin_prof=admin_prof,
	                               a_prof=a_prof, b_prof=b_prof))


def link(client, device, profile):
	"""The user's own profile linked to a device they see (My security profile)."""
	return client.post(f"/inventory/{device}/attributes", data={"sec_profile_id": str(profile)})


# ── The database ─────────────────────────────────────────────────────────────

def test_the_database_refuses_a_link_to_another_users_profile(shared, session_scope):
	"""A link names its user and a profile of that user's: one pointing at
	another user's profile breaks the composite foreign key."""
	with pytest.raises(IntegrityError):
		with session_scope() as s:
			s.add(DeviceCredential(device_id=shared.core, user_id=shared.b.id,
			                       profile_id=shared.a_prof))


# ── Rolling out ──────────────────────────────────────────────────────────────

def test_each_user_rolls_out_to_a_global_device_with_their_own_profile(
		shared, client_for, captured_submits):
	"""Two users rolling out to the same global device log in with their own
	profiles: the admin as admin-user, a as a-user."""
	link(client_for(shared.a), shared.core, shared.a_prof)
	for user in (shared.admin, shared.a):
		client_for(user).post("/rollout/start", data={
			"device_ids": [str(shared.core)], "manual_commands": "hostname x"})
	assert [c.devices[0].username for c in captured_submits] == ["admin-user", "a-user"]


def test_a_rollout_to_a_device_without_a_profile_of_yours_is_refused(
		shared, client_for, captured_submits):
	"""b has no profile linked to CORE: the rollout is refused before
	anything is queued, the flash naming the device."""
	client = client_for(shared.b)
	resp = client.post("/rollout/start", data={
		"device_ids": [str(shared.core)], "manual_commands": "hostname x"})
	assert resp.status_code == 302 and resp.headers["Location"].endswith("/rollout/new")
	assert captured_submits == []
	with client.session_transaction() as s:
		(_, message), = s["_flashes"]
	assert message.startswith("These devices have no security profile of yours: CORE.")


def test_new_rollout_marks_the_devices_without_a_profile_of_yours(shared, client_for):
	"""New Rollout marks CORE as needing a profile for b, not for the admin
	(linked)."""
	html_b = client_for(shared.b).get("/rollout/new").get_data(as_text=True)
	html_admin = client_for(shared.admin).get("/rollout/new").get_data(as_text=True)
	assert "No security profile of yours" in html_b
	assert "No security profile of yours" not in html_admin


# ── Inventory ────────────────────────────────────────────────────────────────

def test_inventory_counts_and_filters_the_devices_needing_a_profile(
		shared, client_for, make_device):
	"""b sees CORE (global, no link of b's) and b's own linked device: the
	note counts 1, the badge is on one card, the filter shows only CORE."""
	make_device(shared.b, ip="10.70.0.2", label="B-OWN", profile_id=shared.b_prof)
	client = client_for(shared.b)
	html = client.get("/inventory").get_data(as_text=True)
	assert "1 device needs a security profile of yours" in html
	assert html.count('class="needs-profile-badge"') == 1
	only = client.get("/inventory?needs_profile=1").get_data(as_text=True)
	assert "CORE" in only and "B-OWN" not in only


def test_my_security_profile_on_a_global_device(shared, client_for, linked, session_scope):
	"""b links their own profile to CORE from its read-only window: b's link
	alone, the admin's untouched, audited; a blank one removes it."""
	client = client_for(shared.b)
	link(client, shared.core, shared.b_prof)
	assert (linked(shared.core, shared.b), linked(shared.core, shared.admin)) == \
	       (shared.b_prof, shared.admin_prof)
	with session_scope() as s:
		(row,) = s.query(AuditLog).filter_by(action="inventory.attributes",
		                                     object_id=shared.core).all()
		assert row.detail["profile_linked"] is True
	client.post(f"/inventory/{shared.core}/attributes", data={"sec_profile_id": ""})
	assert linked(shared.core, shared.b) is None


def test_an_import_links_the_profiles_to_the_importer(client_for, make_user, session_scope):
	"""A CSV import's credentials become the importer's profile, linked for them."""
	user = make_user()
	client_for(user).post("/inventory/import_csv", content_type="multipart/form-data", data={
		"csv_file": (io.BytesIO(b"ip,device_type,port,label,username,password\n"
		                        b"10.70.1.1,cisco_ios,22,imp,u,p\n"), "d.csv"),
		"create_profiles": "on"})
	with session_scope() as s:
		device = s.query(Inventory).filter_by(label="imp").one()
		(credential,) = device.credentials
		assert (credential.user_id, credential.profile.username) == (user.id, "u")


# ── Global <-> private ──────────────────────────────────────────────────────

def test_making_a_global_device_private_keeps_only_the_editors_link(
		shared, client_for, make_user, make_profile, linked, session_scope):
	"""Another admin makes CORE private: it becomes theirs, with their own
	link; the creator's and a's links go."""
	editor = make_user(role="admin")
	editors = make_profile(editor, label="editor-prof")
	link(client_for(shared.a), shared.core, shared.a_prof)
	client_for(editor).post(f"/inventory/{shared.core}/edit", data={
		**FORM, "label": "CORE", "ip": "10.70.0.1", "sec_profile_id": str(editors)})
	with session_scope() as s:
		device = s.get(Inventory, shared.core)
		assert (device.is_global, device.user_id) == (False, editor.id)
		assert {(c.user_id, c.profile_id) for c in device.credentials} == {(editor.id, editors)}
	assert (linked(shared.core, shared.a), linked(shared.core, shared.admin)) == (None, None)


# ── Deleting a user ──────────────────────────────────────────────────────────

def test_deleting_a_global_devices_creator_keeps_it_and_the_others_links(
		shared, client_for, make_user, make_device, linked, session_scope):
	"""Deleting the admin who created CORE: CORE stays global, without a
	creator, with a's link; the admin's private device goes with them."""
	link(client_for(shared.a), shared.core, shared.a_prof)
	private = make_device(shared.admin, ip="10.70.0.3", label="ADMIN-OWN",
	                      profile_id=shared.admin_prof)
	client_for(make_user(role="admin")).post(f"/admin/users/{shared.admin.id}/delete")
	with session_scope() as s:
		assert s.get(User, shared.admin.id) is None
		core = s.get(Inventory, shared.core)
		assert (core.is_global, core.user_id) == (True, None)
		assert s.get(Inventory, private) is None
	assert linked(shared.core, shared.a) == shared.a_prof


# ── Rollback ─────────────────────────────────────────────────────────────────

@pytest.fixture
def owners_job(shared, client_for, make_device, session_scope):
	"""A job of a's that succeeded on CORE (a linked) and on a's own device."""
	own = make_device(shared.a, ip="10.70.0.5", label="A-OWN", profile_id=shared.a_prof)
	link(client_for(shared.a), shared.core, shared.a_prof)
	job = uuid.uuid4()
	add_result(session_scope, shared.a, job, ip="10.70.0.1")
	add_result(session_scope, shared.a, job, ip="10.70.0.5")
	return type("Job", (), dict(id=job, own=own))


def test_an_admins_rollback_asks_for_a_profile_where_they_have_none(
		owners_job, client_for, make_user, make_profile, captured_submits):
	"""An admin without links rolling back a's job: 409 naming both devices -
	CORE linkable, a's own device not - with the admin's profiles to pick
	from; nothing submitted."""
	admin2 = make_user(role="admin")
	prof = make_profile(admin2, label="admin2-prof")
	resp = client_for(admin2).post(f"/rollout/rollback/{owners_job.id}",
	                               json={"commands": "no hostname"})
	assert resp.status_code == 409
	assert {d["label"]: d["linkable"] for d in resp.json["needs_profile"]} == \
	       {"CORE": True, "A-OWN": False}
	assert resp.json["profiles"] == [{"id": str(prof), "label": "admin2-prof"}]
	assert captured_submits == []


def test_a_profile_picked_for_a_rollback_is_linked_to_the_global_device_only(
		shared, owners_job, client_for, make_user, make_profile, linked, captured_submits):
	"""The admin picks a profile with "Link it" ticked: the rollback logs in
	with it on both devices, it's linked to CORE for the admin, and a's own
	device gets no link of the admin's (used for this rollback only)."""
	admin2 = make_user(role="admin")
	prof = make_profile(admin2, label="admin2-prof", username="admin2-user")
	resp = client_for(admin2).post(f"/rollout/rollback/{owners_job.id}", json={
		"commands": "no hostname", "profile_id": str(prof), "link": True})
	assert resp.json["status"] == "ok"
	(call,) = captured_submits
	assert sorted((d.label, d.username) for d in call.devices) == \
	       [("A-OWN", "admin2-user"), ("CORE", "admin2-user")]
	assert (linked(shared.core, admin2), linked(owners_job.own, admin2)) == (prof, None)
	assert linked(owners_job.own, shared.a) == shared.a_prof


def test_a_profile_picked_without_link_is_used_once(
		shared, owners_job, client_for, make_user, make_profile, linked, captured_submits):
	"""With "Link it" unticked the picked profile is used but linked nowhere."""
	admin2 = make_user(role="admin")
	prof = make_profile(admin2)
	client_for(admin2).post(f"/rollout/rollback/{owners_job.id}", json={
		"commands": "no hostname", "profile_id": str(prof), "link": False})
	assert len(captured_submits) == 1
	assert linked(shared.core, admin2) is None


def test_a_rollback_with_another_users_profile_is_refused(
		shared, owners_job, client_for, make_user, captured_submits):
	"""A picked profile that isn't the admin's own: 404, nothing submitted."""
	resp = client_for(make_user(role="admin")).post(f"/rollout/rollback/{owners_job.id}", json={
		"commands": "no hostname", "profile_id": str(shared.b_prof), "link": True})
	assert resp.status_code == 404
	assert captured_submits == []
