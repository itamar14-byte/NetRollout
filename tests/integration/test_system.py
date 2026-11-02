"""The instance route behind the startup reverse-proxy check, the version and
source on every page, Grafana's sign-in check, the Content-Security-Policy
and its reports."""
import json

import pytest

from src.db.tables import User
from src.runtime import VERSION, source_url

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def test_instance_route_returns_this_processes_token(app, client_for):
	"""/_netrollout/instance answers anyone with this process's 32-character
	instance token, and writes no session cookie."""
	client = client_for()   # anonymous: nginx fetches it without a session
	resp = client.get("/_netrollout/instance")
	assert resp.status_code == 200
	assert resp.json == {"instance": app.instance_token}
	assert len(app.instance_token) == 32
	# no session is written for it
	assert "Set-Cookie" not in resp.headers


@pytest.mark.parametrize("path,role", [
	("/", None),                  # base.html (the login page)
	("/dashboard", "operator"),   # operator_base.html
	("/admin/users", "admin"),    # admin.html
])
def test_every_page_skeleton_shows_the_version_and_its_source(
		client_for, make_user, path, role):
	"""AGPL-3.0 §13: network users are offered the source of this version.
	Each page skeleton (base, operator_base, admin) shows the version, links
	source_url() and the licence line."""
	user = make_user(role=role) if role else None
	html = client_for(user).get(path).get_data(as_text=True)
	assert f"NETROLLOUT V{VERSION.upper()}" in html
	assert f'href="{source_url()}"' in html
	assert "LICENSED UNDER GNU AGPL V3" in html


# ── /_netrollout/grafana-auth: nginx's gate in front of Grafana ──────────────
# nginx understands only 2xx / 401 / 403 from it (anything else is a 500)

GRAFANA_AUTH = "/_netrollout/grafana-auth"


def test_grafana_is_for_signed_in_admins(client_for, make_user):
	"""Grafana's auth check: an admin gets 204 with X-NetRollout-User = the
	username, signed out 401, an operator 403 without the user header."""
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
	"""An admin who must change the password gets 403 (the password gate would
	redirect, a 500 for nginx)."""
	admin = make_user(role="admin", must_change_password=True)
	assert client_for(admin).get(GRAFANA_AUTH).status_code == 403


def test_grafana_access_ends_with_the_session(app, client_for, make_user):
	"""Grafana's auth check turns 401 once the session ends: after signing
	out, and after SessionStore.end_for (Terminate Session / a reset)."""
	admin = make_user(role="admin")
	browser = client_for(admin)
	assert browser.get(GRAFANA_AUTH).status_code == 204
	browser.get("/logout")                                     # signed out
	assert browser.get(GRAFANA_AUTH).status_code == 401

	browser = client_for(admin)
	browser.get("/dashboard")                       # a stored Redis session
	with app.app_context():
		app.sessions.end_for(admin.id)              # Terminate Session / reset
	assert browser.get(GRAFANA_AUTH).status_code == 401


def test_grafana_follows_role_and_status_changes(client_for, make_user,
                                                 session_scope):
	"""An open session follows the database: a demoted admin gets 403, a
	deactivated one 401."""
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


# ── The Content-Security-Policy ──────────────────────────────────────────────

CSP_HEADER = "Content-Security-Policy"
CSP_REPORT = {"csp-report": {"document-uri": "https://nr.corp/inventory", "violated-directive": "script-src-elem",
                             "blocked-uri": "inline", "source-file": "https://nr.corp/inventory", "line-number": 12}}


@pytest.mark.parametrize("path,role", [
	("/", None),                  # base.html
	("/dashboard", "operator"),   # operator_base.html
	("/admin/users", "admin"),    # admin.html
])
def test_every_page_carries_the_content_security_policy(client_for, make_user, path, role):
	"""Each skeleton's pages are answered with the policy (Flask's own header,
	so also without nginx): scripts from NetRollout only, reports to
	/_netrollout/csp-report."""
	client = client_for(make_user(role=role) if role else None)
	resp = client.get(path)
	policy = resp.headers.get(CSP_HEADER, "")
	assert resp.status_code == 200 and "script-src 'self';" in policy
	assert "report-uri /_netrollout/csp-report" in policy


def test_a_csp_report_is_logged(client_for, capsys):
	"""A browser's report (application/csp-report, no CSRF token, no session)
	gets 204 and one line in the log naming what was blocked where; no
	session cookie is written for it."""
	resp = client_for().post("/_netrollout/csp-report", data=json.dumps(CSP_REPORT),
	                         content_type="application/csp-report")
	assert resp.status_code == 204 and "Set-Cookie" not in resp.headers
	line = [l for l in capsys.readouterr().out.splitlines() if "Content-Security-Policy blocked" in l]
	assert len(line) == 1
	assert "violated-directive='script-src-elem'" in line[0] and "blocked-uri='inline'" in line[0]
	assert "document-uri='https://nr.corp/inventory'" in line[0] and "line-number='12'" in line[0]


def test_a_csp_report_too_big_or_not_a_report_is_refused(client_for, capsys):
	"""Over 8 KB: 413, unread; not JSON or without csp-report: 400. Nothing is logged."""
	client = client_for()
	big = {"csp-report": {"blocked-uri": "x" * 9000}}
	assert client.post("/_netrollout/csp-report", data=json.dumps(big),
	                   content_type="application/csp-report").status_code == 413
	assert client.post("/_netrollout/csp-report", data="not json",
	                   content_type="application/csp-report").status_code == 400
	assert client.post("/_netrollout/csp-report", data=json.dumps({"other": 1}),
	                   content_type="application/json").status_code == 400
	assert "Content-Security-Policy blocked" not in capsys.readouterr().out
