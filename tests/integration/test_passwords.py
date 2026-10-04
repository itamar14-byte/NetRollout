"""Stage 4 through the routes: the password rule at registration, the forced
change gate, changing a password (forced and voluntary) and the admin reset
with a temporary password."""
import pytest
from werkzeug.security import check_password_hash

from src.db.tables import AuditLog, User
from src.passwords import password_problem
from tests.integration.conftest import TEST_PASSWORD

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

NEW_PASSWORD = "Fresh-pass-42"


def get_user(session_scope, user_id):
	with session_scope() as s:
		user = s.get(User, user_id)
		s.expunge(user)
		return user


def audits(session_scope, action):
	with session_scope() as s:
		return [(a.actor_username, a.success, a.detail)
		        for a in s.query(AuditLog).filter_by(action=action)
		        .order_by(AuditLog.timestamp)]


def change(client, current=TEST_PASSWORD, new=NEW_PASSWORD, confirm=None):
	return client.post("/account/password", data={
		"current_password": current, "new_password": new,
		"confirm_password": new if confirm is None else confirm})


@pytest.fixture
def flagged(make_user):
	return make_user(must_change_password=True)


# ── Registration uses the rule ───────────────────────────────────────────────

@pytest.mark.parametrize("password", ["Short1", "lettersonly", "12345678",
                                      "pässword123"])
def test_registration_refuses_a_weak_password(client_for, session_scope,
                                              password):
	resp = client_for().post("/register", data={
		"username": "weakling", "password": password, "email": "w@x.io",
		"full_name": "Weak"})
	assert resp.headers["Location"] == "/register"
	with session_scope() as s:
		assert s.query(User).filter_by(username="weakling").count() == 0


def test_registration_refuses_the_username_inside_the_password(client_for,
                                                               session_scope):
	client_for().post("/register", data={
		"username": "carol", "password": "Carol2024x", "email": "c@x.io",
		"full_name": "Carol"})
	with session_scope() as s:
		assert s.query(User).filter_by(username="carol").count() == 0


# ── The gate ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("page", ["/dashboard", "/inventory/", "/account",
                                  "/rollout/new"])
def test_flagged_user_is_sent_to_the_change_page(flagged, client_for, page):
	resp = client_for(flagged).get(page)
	assert resp.status_code == 302
	assert resp.headers["Location"] == "/account/password"


def test_flagged_user_gets_403_json_from_fetch_calls(flagged, client_for):
	resp = client_for(flagged, xhr=True).post("/inventory/reachability",
	                                          json={"device_ids": []})
	assert resp.status_code == 403
	assert resp.json["redirect"] == "/account/password"


@pytest.mark.parametrize("path, code", [("/account/password", 200),
                                        ("/_netrollout/health", 200),
                                        ("/_netrollout/instance", 200),
                                        ("/static/logo.svg", 200),
                                        ("/logout", 302)])
def test_what_a_flagged_user_can_still_reach(flagged, client_for, path, code):
	resp = client_for(flagged).get(path)
	assert resp.status_code == code
	assert resp.headers.get("Location") != "/account/password"


def test_the_seeded_admin_is_gated_after_signing_in(client_for, make_user):
	# the factory admin skips 2FA; the dashboard it lands on sends it on
	make_user(username="admin", role="admin", must_change_password=True)
	client = client_for()
	assert client.post("/login", data={"username": "admin",
	                                   "password": TEST_PASSWORD}) \
		       .headers["Location"] == "/dashboard"
	assert client.get("/dashboard").headers["Location"] == "/account/password"
	page = client.get("/account/password").data.decode()
	assert "The factory password must be changed" in page


def test_unflagged_user_is_not_gated(make_user, client_for):
	assert client_for(make_user()).get("/dashboard").status_code == 200


# ── Changing the password ────────────────────────────────────────────────────

def test_forced_change_clears_the_flag_and_rotates_the_session(
		flagged, client_for, session_scope, app):
	client = client_for(flagged)
	client.get("/account/password")                     # session saved
	before = client.get_cookie("session").value
	resp = change(client)
	assert resp.headers["Location"] == "/dashboard"
	after = client.get_cookie("session").value
	assert after != before                              # a new session id
	assert not app.backend.redis.client.exists(f"redis_session:{before}")
	user = get_user(session_scope, flagged.id)
	assert user.must_change_password is False
	assert check_password_hash(user.password_hash, NEW_PASSWORD)
	assert client.get("/dashboard").status_code == 200  # still signed in
	assert audits(session_scope, "auth.password_change") == [
		(flagged.username, True, {"forced": True, "other_sessions_ended": 0})]


def test_voluntary_change_from_the_account_page(make_user, client_for,
                                                session_scope):
	user = make_user()
	client = client_for(user)
	assert 'href="/account/password"' in client.get("/account").data.decode()
	assert change(client).headers["Location"] == "/dashboard"
	assert check_password_hash(get_user(session_scope, user.id).password_hash,
	                           NEW_PASSWORD)
	assert audits(session_scope, "auth.password_change")[0][2] == \
	       {"forced": False, "other_sessions_ended": 0}


def signed_in_clients(client_for, user, count):
	"""`count` separate browsers signed in as `user`, each with a session
	stored in Redis (user_session:<id> points at the last one only)."""
	clients = [client_for(user) for _ in range(count)]
	for c in clients:
		c.get("/account/password")          # allowed even while flagged
	return clients


def sid_of(client):
	return client.get_cookie("session").value


def test_a_change_signs_out_every_other_session(make_user, client_for, app):
	user = make_user()
	me, other_browser = signed_in_clients(client_for, user, 2)
	stolen = sid_of(other_browser)
	assert change(me).headers["Location"] == "/dashboard"
	assert not app.backend.redis.client.exists(f"redis_session:{stolen}")
	assert other_browser.get("/dashboard").headers["Location"] \
	       .startswith("/?")                               # to sign in
	assert me.get("/dashboard").status_code == 200       # still signed in


# (wrong_current moved to the voluntary change below: a forced change no
# longer asks for the current password — approved 2026-10-04)
@pytest.mark.parametrize("kwargs, reason", [
	({"confirm": "Other-pass-42"}, "mismatch"),
	({"new": "short1"}, "rule"),
	({"new": TEST_PASSWORD}, "rule"),                  # same as the current
])
def test_a_refused_change_keeps_everything(flagged, client_for, session_scope,
                                           kwargs, reason):
	client = client_for(flagged)
	assert change(client, **kwargs).headers["Location"] == "/account/password"
	user = get_user(session_scope, flagged.id)
	assert user.must_change_password is True
	assert check_password_hash(user.password_hash, TEST_PASSWORD)
	((_, success, detail),) = audits(session_scope, "auth.password_change")
	assert success is False and detail["reason"] == reason


def test_a_voluntary_change_needs_the_current_password(make_user, client_for,
                                                       session_scope):
	user = make_user()
	client = client_for(user)
	assert change(client, current="wrong-Pass-1").headers["Location"] == 	       "/account/password"
	assert check_password_hash(get_user(session_scope, user.id).password_hash,
	                           TEST_PASSWORD)
	((_, success, detail),) = audits(session_scope, "auth.password_change")
	assert success is False and detail["reason"] == "wrong_current"


def test_a_forced_change_does_not_ask_for_the_current_password(
		flagged, client_for, session_scope):
	# the sign-in that just happened proved it (factory admin / reset)
	client = client_for(flagged)
	page = client.get("/account/password").get_data(as_text=True)
	assert 'name="current_password"' not in page
	resp = client.post("/account/password", data={
		"new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
	assert resp.headers["Location"] == "/dashboard"
	user = get_user(session_scope, flagged.id)
	assert user.must_change_password is False
	assert check_password_hash(user.password_hash, NEW_PASSWORD)


def test_a_forced_change_still_refuses_the_current_password(
		flagged, client_for, session_scope):
	client = client_for(flagged)
	resp = client.post("/account/password", data={
		"new_password": TEST_PASSWORD, "confirm_password": TEST_PASSWORD})
	assert resp.headers["Location"] == "/account/password"
	assert get_user(session_scope, flagged.id).must_change_password is True
	((_, success, detail),) = audits(session_scope, "auth.password_change")
	assert success is False and detail["reason"] == "rule"   # not "wrong_current"


def test_the_voluntary_page_asks_for_the_current_password(make_user,
                                                          client_for):
	page = client_for(make_user()).get("/account/password").get_data(as_text=True)
	assert 'name="current_password"' in page


def test_ldap_users_are_told_to_use_the_directory(make_user, client_for):
	user = make_user(auth_type="ldap")
	resp = client_for(user).get("/account/password")
	assert resp.headers["Location"] == "/account"
	assert "Managed by your directory" in \
	       client_for(user).get("/account").data.decode()


# ── Admin reset ──────────────────────────────────────────────────────────────

@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def reset(client, user_id):
	return client.post(f"/admin/users/{user_id}/reset_password")


def test_reset_gives_a_temporary_password_and_forces_a_change(
		admin, make_user, client_for, session_scope, app):
	target = make_user()
	target_client = client_for(target)
	target_client.get("/dashboard")                     # has a live session
	sid = target_client.get_cookie("session").value
	app.backend.redis.client.set(f"user_session:{target.id}", sid)

	resp = reset(client_for(admin), target.id)
	assert resp.status_code == 200
	temporary = resp.json["temporary_password"]
	assert password_problem(temporary, target.username) is None

	user = get_user(session_scope, target.id)
	assert user.must_change_password is True
	assert check_password_hash(user.password_hash, temporary)
	assert not check_password_hash(user.password_hash, TEST_PASSWORD)
	assert not app.backend.redis.client.exists(f"user_session:{target.id}")
	assert not app.backend.redis.client.exists(f"redis_session:{sid}")
	# Signed out: sent to sign in, not to the change page (the flag alone
	# would redirect there too)
	assert target_client.get("/dashboard").headers["Location"] \
	       != "/account/password"
	(entry,) = audits(session_scope, "user.reset_password")
	assert entry[0] == admin.username and temporary not in str(entry)


def test_reset_signs_the_user_out_everywhere(admin, make_user, client_for,
                                              session_scope, app):
	target = make_user()
	browsers = signed_in_clients(client_for, target, 3)
	sids = [sid_of(b) for b in browsers]
	# the pointer knows only the latest sign-in; the others must go too
	app.backend.redis.client.set(f"user_session:{target.id}", sids[-1])
	assert reset(client_for(admin), target.id).status_code == 200
	for sid, browser in zip(sids, browsers):
		assert not app.backend.redis.client.exists(f"redis_session:{sid}")
		assert browser.get("/dashboard").headers["Location"].startswith("/?")
	(entry,) = audits(session_scope, "user.reset_password")
	assert entry[2] == {"sessions_ended": 3}


def test_reset_leaves_other_users_signed_in(admin, make_user, client_for):
	target, bystander = make_user(), make_user()
	(bystander_browser,) = signed_in_clients(client_for, bystander, 1)
	reset(client_for(admin), target.id)
	assert bystander_browser.get("/dashboard").status_code == 200


@pytest.mark.parametrize("target_kind, message", [
	("self", "Use Change password"),
	("factory", "factory admin"),
	("ldap", "LDAP"),
])
def test_reset_refusals(admin, make_user, client_for, target_kind, message):
	target = {"self": lambda: admin,
	          "factory": lambda: make_user(username="admin", role="admin"),
	          "ldap": lambda: make_user(auth_type="ldap")}[target_kind]()
	resp = reset(client_for(admin), target.id)
	assert resp.status_code == 400 and message in resp.json["message"]


def test_reset_is_admin_only(make_user, client_for, session_scope):
	target = make_user()
	resp = reset(client_for(make_user(), xhr=True), target.id)
	assert resp.status_code in (302, 403)
	assert get_user(session_scope, target.id).must_change_password is False
