"""Admin -> Users: user management (actions, bulk, 2FA reset), live sessions
and Kick, Terminate Session. Never called here, even as admin:
/admin/server/restart (os._exit)."""
import html
import json
import re
import time
import uuid

import pytest

from src.db.tables import AuditLog, User
from src.encryption import encrypt
from tests.integration.conftest import TEST_PASSWORD


pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


def get_user(session_scope, user_id):
	"""The user row by id, detached from its session (None when gone)."""
	with session_scope() as s:
		u = s.get(User, user_id)
		if u:
			s.expunge(u)
		return u


# ── User management ──────────────────────────────────────────────────────────

def test_panel_and_users_page(admin, client_for):
	"""/admin redirects to /admin/users, which renders."""
	c = client_for(admin)
	assert c.get("/admin").headers["Location"] == "/admin/users"
	assert c.get("/admin/users").status_code == 200


@pytest.mark.parametrize("action,expected", [
	("approve", {"is_approved": True, "is_active": True}),
	("disable", {"is_active": False}),
	("promote", {"role": "admin", "is_approved": True}),
	("demote", {"role": "operator"}),
])
def test_user_actions(admin, client_for, make_user, session_scope, action,
                      expected):
	"""Each user action sets its fields and is audited once as user.<action>
	(cases: approve, disable, promote, demote)."""
	target = make_user(approved=False, active=False) if action == "approve" \
		else make_user(role="admin" if action == "demote" else "operator")
	client_for(admin).post(f"/admin/users/{target.id}/{action}")
	user = get_user(session_scope, target.id)
	for field, value in expected.items():
		assert getattr(user, field) == value, field
	with session_scope() as s:
		assert s.query(AuditLog).filter_by(action=f"user.{action}",
		                                   object_id=target.id).count() == 1


def test_admin_cannot_disable_or_delete_self(admin, client_for, session_scope):
	"""An admin's disable and delete of their own account leave it in place
	and active."""
	c = client_for(admin)
	c.post(f"/admin/users/{admin.id}/disable")
	c.post(f"/admin/users/{admin.id}/delete")
	me = get_user(session_scope, admin.id)
	assert me is not None and me.is_active is True


def test_factory_admin_is_untouchable(admin, client_for, make_user,
                                      session_scope):
	"""The factory "admin" account can't be demoted or deleted: it stays an
	admin."""
	factory = make_user(username="admin", role="admin")
	client_for(admin).post(f"/admin/users/{factory.id}/demote")
	client_for(admin).post(f"/admin/users/{factory.id}/delete")
	kept = get_user(session_scope, factory.id)
	assert kept is not None and kept.role == "admin"


def test_bulk_action(admin, client_for, make_user, session_scope):
	"""A bulk disable disables the selected users but skips the admin doing it."""
	a, b = make_user(), make_user()
	client_for(admin).post("/admin/users/bulk/disable",
	                       data={"user_ids": f"{a.id},{b.id},{admin.id}"})
	assert not get_user(session_scope, a.id).is_active
	assert not get_user(session_scope, b.id).is_active
	assert get_user(session_scope, admin.id).is_active  # self skipped


def test_bulk_reset_2fa(admin, app, client_for, make_user, session_scope):
	"""Bulk Reset 2FA: the Users page offers it and knows who has 2FA; both
	secrets are cleared, one audit entry names both users, and the next
	sign-in goes to enrollment."""
	a, b = make_user(), make_user()
	with session_scope() as s:
		for uid in (a.id, b.id):
			s.get(User, uid).otp_secret = encrypt("JBSWY3DPEHPK3PXP")
	# the Users page offers the action and knows who has 2FA
	page = client_for(admin).get("/admin/users").get_data(as_text=True)
	assert 'id="btnReset2FA"' in page and '"has_2fa": true' in page
	client_for(admin).post("/admin/users/bulk/reset_2fa",
	                       data={"user_ids": f"{a.id},{b.id}"})
	assert get_user(session_scope, a.id).otp_secret is None
	assert get_user(session_scope, b.id).otp_secret is None
	with session_scope() as s:
		entry = s.query(AuditLog).filter_by(action="user.bulk_reset_2fa").one()
		assert sorted(entry.detail["users"]) == sorted([a.username, b.username])
	# next sign-in goes to enrollment, not verification
	resp = app.test_client().post("/login", data={"username": a.username,
	                                              "password": TEST_PASSWORD})
	assert resp.headers["Location"] == "/otp_enroll"


def test_reset_2fa_is_admin_only(client_for, make_user, session_scope):
	"""An operator's bulk Reset 2FA leaves the user's 2FA secret in place."""
	victim = make_user()
	with session_scope() as s:
		s.get(User, victim.id).otp_secret = encrypt("JBSWY3DPEHPK3PXP")
	client_for(make_user()).post("/admin/users/bulk/reset_2fa",
	                             data={"user_ids": str(victim.id)})
	assert get_user(session_scope, victim.id).otp_secret is not None


def test_live_sessions_list_and_kick(app, admin, client_for, make_user):
	"""Live Sessions lists a signed-in user; Kick ends the session (its key
	gone from Redis); kicking a user without a session is 404."""
	target = make_user()
	browser = client_for(target)
	browser.get("/dashboard")
	sid = browser.get_cookie("session").value
	c = client_for(admin)
	assert target.username in c.get("/admin/sessions").get_data(as_text=True)
	assert c.post(f"/admin/sessions/{target.id}/kick").json["status"] == "ok"
	assert app.backend.redis.client.get(f"redis_session:{sid}") is None
	assert c.post(f"/admin/sessions/{uuid.uuid4()}/kick").status_code == 404


def test_kick_signs_the_user_out_of_every_browser(admin, client_for, make_user):
	"""A user signed in on two computers is signed out of both by one Kick:
	each one's next page goes to the sign-in page, and the open page's
	background session check is redirected there too (so it leaves within
	30 s)."""
	target = make_user()
	office, laptop = client_for(target), client_for(target)
	assert office.get("/dashboard").status_code == 200
	assert laptop.get("/dashboard").status_code == 200
	assert client_for(admin).post(f"/admin/sessions/{target.id}/kick").status_code == 200
	for browser in (office, laptop):
		assert browser.get("/dashboard").status_code == 302
		# the open page's 30 s check: redirected to the sign-in page, which
		# makes the page leave (_idle_timeout.html: r.redirected)
		check = browser.get("/account/session", headers={"X-NR-Background": "1"})
		assert (check.status_code, check.headers["Location"]) == (
			302, "/?next=%2Faccount%2Fsession")


def test_live_sessions_leave_out_sessions_that_ended(admin, client_for, make_user):
	"""A session past its idle limit (ended, though its browser hasn't come
	back to be told) isn't listed on Live Sessions, nor shown as signed in on
	Users; a live one is."""
	active, idle = make_user(), make_user()
	client_for(active).get("/dashboard")
	gone = client_for(idle)
	gone.get("/dashboard")
	with gone.session_transaction() as s:      # last seen 20 minutes ago
		s["nr_last_active"] = time.time() - 20 * 60
	c = client_for(admin)
	page = c.get("/admin/sessions").get_data(as_text=True)
	assert active.username in page and idle.username not in page
	rows = [json.loads(html.unescape(raw)) for raw in re.findall(
		r"data-user='([^']*)'", c.get("/admin/users").get_data(as_text=True))]
	signed_in = {r["username"]: r["has_session"] for r in rows}
	assert signed_in[active.username] is True
	assert signed_in[idle.username] is False


def test_terminate_session_signs_the_user_out_everywhere(
		admin, make_user, client_for, app):
	"""Terminate Session removes all the user's sessions (both browsers): the
	next page and the background session check redirect to the sign-in."""
	target = make_user()
	browsers = [client_for(target) for _ in range(2)]
	for b in browsers:
		b.get("/dashboard")
	sids = [b.get_cookie("session").value for b in browsers]
	app.backend.redis.client.set(f"user_session:{target.id}", sids[-1])
	client_for(admin).post(f"/admin/users/{target.id}/terminate_session")
	assert not any(app.backend.redis.client.exists(f"redis_session:{s}")
	               for s in sids)
	# ...and really out: the next page goes to sign-in, and the page's own
	# background check (every 30 s) is refused too
	for b in browsers:
		assert b.get("/dashboard").headers["Location"].startswith("/?next=")
		check = b.get("/account/session", headers={"X-NR-Background": "1"})
		assert check.status_code == 302 and check.headers["Location"].startswith("/?next=")


def test_unknown_user_actions_are_404_and_not_audited(admin, client_for,
                                                      make_user, session_scope):
	"""An action that doesn't exist, on one user or in bulk, is 404 and leaves
	no audit row."""
	target = make_user()
	c = client_for(admin)
	assert c.post(f"/admin/users/{target.id}/explode").status_code == 404
	assert c.post("/admin/users/bulk/explode",
	              data={"user_ids": str(target.id)}).status_code == 404
	with session_scope() as s:
		assert not s.query(AuditLog).filter(AuditLog.action.like("user.%explode")).count()
