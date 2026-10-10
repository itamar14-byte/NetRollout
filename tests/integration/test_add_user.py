"""Admin -> Users -> Add user (Request access + Approve in one step), the
shared checks with Request access, and the sidebar's count of requests."""
import pytest
from werkzeug.security import check_password_hash

from src.accounts.users import password_problem
from src.db.tables import AuditLog, User

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

FORM = {"username": "dana", "email": "dana@corp.example", "full_name": "Dana Levi",
        "position": "NOC"}


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def add(client, **changes):
	return client.post("/admin/users/new", json={**FORM, **changes})


def test_an_added_user_is_approved_with_a_temporary_password(admin, client_for, session_scope):
	"""Add user creates an approved, active local operator with the form's
	details and a temporary password that passes the rule (returned once and
	stored hashed), must_change_password set, audited user.created by the
	admin with role operator."""
	resp = add(client_for(admin, xhr=True))
	assert resp.status_code == 200, resp.json
	assert resp.json["username"] == "dana" and resp.json["role"] == "operator"
	temporary = resp.json["temporary_password"]
	assert password_problem(temporary, "dana") is None
	with session_scope() as s:
		u = s.query(User).filter_by(username="dana").one()
		assert (u.role, u.is_approved, u.is_active, u.must_change_password, u.auth_type) == \
		       ("operator", True, True, True, "local")
		assert (u.email, u.full_name, u.position) == ("dana@corp.example", "Dana Levi", "NOC")
		assert check_password_hash(u.password_hash, temporary)
		audit = s.query(AuditLog).filter_by(action="user.created").one()
		assert (audit.actor_username, audit.object_label, audit.detail) == \
		       (admin.username, "dana", {"role": "operator"})


def test_an_admin_can_be_added(admin, client_for, session_scope):
	"""Add user with role admin creates an admin."""
	assert add(client_for(admin, xhr=True), role="admin").json["role"] == "admin"
	with session_scope() as s:
		assert s.query(User).filter_by(username="dana").one().role == "admin"


@pytest.mark.parametrize("changes, message", [
	({"username": ""}, "Username is required"),
	({"email": "  "}, "Email is required"),
	({"full_name": ""}, "Full name is required"),
	({"email": "dana.corp.example"}, "isn't valid"),
	({"username": "d" * 65}, "at most 64"),
	({"username": "<b>dana</b>"}, "letters, digits"),
	({"username": "dana smith"}, "letters, digits"),
	({"username": ".dana"}, "letters, digits"),
	({"role": "superuser"}, "operator or admin"),
])
def test_refused_in_words(admin, client_for, session_scope, changes, message):
	"""A missing username / email / full name, an email without @, a username
	over 64 characters or with other characters than letters, digits, . _ -
	(starting with a letter or digit), or an unknown role is refused with 422
	and a message saying why; no user is created (the admin is the only one)."""
	resp = add(client_for(admin, xhr=True), **changes)
	assert resp.status_code == 422 and message in resp.json["message"]
	with session_scope() as s:
		assert s.query(User).filter(User.id != admin.id).count() == 0


def test_a_taken_username_or_email_is_refused(admin, client_for, make_user):
	"""Add user refuses a username already taken ("username is taken") and an
	email already in use ("already in use")."""
	taken = make_user(username="dana")
	c = client_for(admin, xhr=True)
	assert "username is taken" in add(c).json["message"]
	assert "already in use" in add(c, username="dana2", email=f"{taken.username}@test.local").json["message"]


def test_a_username_taken_in_other_capitals_is_refused(admin, client_for, make_user):
	"""A new local account can't be "Dana" next to "dana" - one person, one name
	(sign-in treats a directory's names case-insensitively too)."""
	make_user(username="dana")
	resp = add(client_for(admin, xhr=True), username="Dana", email="d2@corp.example")
	assert resp.status_code == 422 and "username is taken" in resp.json["message"]


def test_operators_cant_add_users(make_user, client_for):
	"""An operator's Add user request is refused (302 or 403)."""
	assert add(client_for(make_user(), xhr=True)).status_code in (302, 403)


def test_request_access_uses_the_same_checks(client_for, session_scope, make_user):
	"""Request access shows the same messages as Add user: a taken username and
	a username over 64 characters (that request creating no user)."""
	make_user(username="dana")
	resp = client_for().post("/register", data={**FORM, "username": "dana", "email": "x@y.io",
	                                            "password": "Str0ng-pass"}, follow_redirects=True)
	assert b"That username is taken." in resp.data
	resp = client_for().post("/register", data={**FORM, "username": "e" * 65, "email": "e@y.io",
	                                            "password": "Str0ng-pass"}, follow_redirects=True)
	assert b"at most 64 characters" in resp.data
	resp = client_for().post("/register", data={**FORM, "username": "<i>e</i>", "email": "e@y.io",
	                                            "password": "Str0ng-pass"}, follow_redirects=True)
	assert b"letters, digits" in resp.data
	with session_scope() as s:
		assert s.query(User).filter_by(email="e@y.io").count() == 0


def test_the_sidebar_counts_the_requests_waiting(admin, client_for, make_user):
	"""With no access requests the admin sidebar has no badge; with two waiting,
	every admin page shows the badge with 2 and "2 access requests waiting"."""
	page = client_for(admin).get("/admin/users").data.decode()
	assert 'id="pendingRequests"' not in page                      # none: no badge
	make_user(approved=False, active=False)
	make_user(approved=False, active=False)
	page = client_for(admin).get("/admin/settings").data.decode()   # every admin page
	assert '<span class="adm-sb-count" id="pendingRequests"' in page
	assert "2 access requests waiting" in page
	assert ">2</span>" in page


@pytest.mark.parametrize("name", ["netrollout-grafana-admin", "NetRollout-Grafana-Admin"])
def test_the_reserved_username_is_refused_in_its_own_words(admin, client_for, session_scope, name):
	"""Grafana's own administrator's name can't be a NetRollout account (Grafana
	would sign that person in as its administrator): Add user and Request access
	refuse it, in any capitals, saying it's reserved - not that it's taken - and
	create no account."""
	resp = add(client_for(admin, xhr=True), username=name, email="g@corp.example")
	assert resp.status_code == 422
	assert "reserved" in resp.json["message"] and "taken" not in resp.json["message"]
	resp = client_for().post("/register", data={**FORM, "username": name, "email": "g2@y.io",
	                                            "password": "Str0ng-pass"}, follow_redirects=True)
	assert b"reserved" in resp.data and b"taken" not in resp.data
	with session_scope() as s:
		assert s.query(User).filter(User.username.ilike(name)).count() == 0
