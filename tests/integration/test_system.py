"""The instance route behind the startup reverse-proxy check."""
import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def test_instance_route_returns_this_processes_token(app, client_for):
	client = client_for()   # anonymous: nginx fetches it without a session
	resp = client.get("/_netrollout/instance")
	assert resp.status_code == 200
	assert resp.json == {"instance": app.config["INSTANCE_TOKEN"]}
	assert len(app.config["INSTANCE_TOKEN"]) == 32
	# no session is written for it
	assert "Set-Cookie" not in resp.headers


@pytest.mark.parametrize("path,role", [
	("/", None),                  # base.html (the login page)
	("/dashboard", "operator"),   # operator_base.html
	("/admin/users", "admin"),    # admin.html
])
def test_every_page_skeleton_shows_the_version_and_its_source(
		client_for, make_user, path, role):
	# AGPL-3.0 §13: network users are offered the source of this version
	from src.runtime import VERSION, source_url
	user = make_user(role=role) if role else None
	html = client_for(user).get(path).get_data(as_text=True)
	assert f"NETROLLOUT V{VERSION.upper()}" in html
	assert f'href="{source_url()}"' in html
	assert "LICENSED UNDER GNU AGPL V3" in html


# ── /_netrollout/grafana-auth: nginx's gate in front of Grafana ──────────────
# nginx understands only 2xx / 401 / 403 from it (anything else is a 500)

GRAFANA_AUTH = "/_netrollout/grafana-auth"


def test_grafana_is_for_signed_in_admins(client_for, make_user):
	admin = make_user(role="admin")
	resp = client_for(admin).get(GRAFANA_AUTH)
	assert resp.status_code == 204
	assert resp.headers["X-NetRollout-User"] == admin.username   # → Grafana's user
	assert client_for().get(GRAFANA_AUTH).status_code == 401       # → sign in
	operator = client_for(make_user(role="operator")).get(GRAFANA_AUTH)
	assert operator.status_code == 403
	assert "X-NetRollout-User" not in operator.headers


def test_grafana_refuses_an_admin_who_must_change_the_password(client_for,
                                                                make_user):
	# the password gate would redirect (a 500 for nginx): 403 instead
	admin = make_user(role="admin", must_change_password=True)
	assert client_for(admin).get(GRAFANA_AUTH).status_code == 403


def test_grafana_access_ends_with_the_session(app, client_for, make_user):
	from src.webapp.utils import end_user_sessions
	admin = make_user(role="admin")
	browser = client_for(admin)
	assert browser.get(GRAFANA_AUTH).status_code == 204
	browser.get("/logout")                                     # signed out
	assert browser.get(GRAFANA_AUTH).status_code == 401

	browser = client_for(admin)
	browser.get("/dashboard")                       # a stored Redis session
	with app.app_context():
		end_user_sessions(admin.id)                 # Terminate Session / reset
	assert browser.get(GRAFANA_AUTH).status_code == 401


def test_grafana_follows_role_and_status_changes(client_for, make_user,
                                                 session_scope):
	from src.db.tables import User
	admin = make_user(role="admin")
	browser = client_for(admin)
	assert browser.get(GRAFANA_AUTH).status_code == 204
	with session_scope() as s:
		s.get(User, admin.id).role = "user"                  # demoted
	assert browser.get(GRAFANA_AUTH).status_code == 403
	with session_scope() as s:
		user = s.get(User, admin.id)
		user.role, user.is_active = "admin", False           # deactivated
	assert browser.get(GRAFANA_AUTH).status_code == 401
