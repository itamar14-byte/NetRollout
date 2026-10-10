"""Check 5: Grafana through nginx at /grafana/ - for signed-in NetRollout
admins only, each as an Editor under their own name; nothing a browser
sends can choose who Grafana sees; the shipped folders are read-only."""
import base64
import uuid

import pytest

from tests.e2e.harness import Browser, path_of

ADMINS_ONLY = b"Monitoring is for NetRollout administrators."
SHIPPED_FOLDERS = ("netrollout", "netrollout-operations", "netrollout-jobs",
                   "netrollout-security")


def new_user(install, admin, username: str, role: str) -> Browser:
	"""A user added by an admin (Admin -> Users -> Add user), signed in as a
	browser would: the temporary password, the authenticator's code, a new
	password.

	:returns: the user's browser, signed in"""
	token = admin.csrf("/admin/users")
	added = admin.post_json("/admin/users/new", {
		"username": username, "email": f"{username}@e2e.netrollout.test",
		"full_name": f"E2E {role}", "position": "e2e", "role": role}, csrf=token)
	assert added.status == 200, added.text
	temporary = added.json()["temporary_password"]
	secret = install.give_authenticator(username)
	browser = Browser(install)
	answer = browser.sign_in_with_code(install, username, temporary, secret)
	assert answer.status == 302, answer
	answer = browser.change_password("Second-check-42")
	assert answer.status == 302 and path_of(answer.location) == "/dashboard", answer
	return browser


@pytest.fixture(scope="module")
def operator(install, admin):
	return new_user(install, admin, "e2e-operator", "operator")


@pytest.fixture(scope="module")
def second_admin(install, admin):
	return new_user(install, admin, "e2e-admin", "admin")


def basic(user: str, password: str) -> dict[str, str]:
	return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def grafana_user(browser: Browser, headers: dict[str, str] | None = None) -> dict:
	answer = browser.get("/grafana/api/user", headers)
	assert answer.status == 200, (answer.status, answer.location, answer.text[:300])
	return answer.json()


def test_anonymous_is_sent_to_sign_in_and_back_to_grafana(install):
	answer = Browser(install).get("/grafana/")
	assert answer.status == 302 and answer.location == "/?next=/grafana/"
	# the sign-in page keeps it: after signing in, Grafana
	assert Browser(install).get("/grafana/d/anything").location == "/?next=/grafana/"


def test_an_operator_is_refused(install, operator):
	for path in ("/grafana/", "/grafana/api/user", "/grafana/api/search"):
		answer = operator.get(path)
		assert answer.status == 403, path
		assert ADMINS_ONLY in answer.body
	# signed in all the same: NetRollout itself serves the operator
	assert operator.get("/dashboard").status == 200


def test_an_admin_is_an_editor_under_their_own_name(install, second_admin):
	assert second_admin.get("/grafana/").status == 200
	me = grafana_user(second_admin)
	assert me["login"] == "e2e-admin"
	assert me["isGrafanaAdmin"] is False
	orgs = second_admin.get("/grafana/api/user/orgs").json()
	assert [o["role"] for o in orgs] == ["Editor"]


def test_a_forged_user_header_is_replaced(install, second_admin):
	"""The header Grafana trusts is always nginx's: a browser's own is
	replaced when signed in, and gets nobody in when not."""
	forged = {"X-WEBAUTH-USER": "admin"}
	assert grafana_user(second_admin, forged)["login"] == "e2e-admin"
	answer = Browser(install).get("/grafana/api/user", forged)
	assert answer.status == 302 and path_of(answer.location) == "/"


def test_grafanas_own_login_is_ignored(install, second_admin):
	"""Basic auth with Grafana's own admin password gets nobody in, and a
	signed-in admin stays themselves."""
	auth = basic("admin", install.env()["GRAFANA_ADMIN_PASSWORD"])
	answer = Browser(install).get("/grafana/api/user", auth)
	assert answer.status == 302 and path_of(answer.location) == "/"
	me = grafana_user(second_admin, auth)
	assert me["login"] == "e2e-admin" and me["isGrafanaAdmin"] is False


def dashboard(folder: str) -> dict:
	return {"dashboard": {"uid": f"e2e-{uuid.uuid4().hex[:12]}", "title":
	                      f"e2e probe {uuid.uuid4().hex[:6]}", "panels": [],
	                      "schemaVersion": 39},
	        "folderUid": folder, "overwrite": False}


def test_the_shipped_folders_are_read_only_and_custom_is_editable(install, second_admin):
	for uid in SHIPPED_FOLDERS:
		folder = second_admin.get(f"/grafana/api/folders/{uid}")
		assert folder.status == 200, (uid, folder.status)
		assert folder.json()["canEdit"] is False, uid
	assert second_admin.get("/grafana/api/folders/custom").json()["canEdit"] is True

	# the same request: refused in a shipped folder, saved in Custom
	refused = second_admin.post_json("/grafana/api/dashboards/db",
	                                 dashboard("netrollout-operations"))
	assert refused.status == 403, (refused.status, refused.text[:300])
	saved = second_admin.post_json("/grafana/api/dashboards/db", dashboard("custom"))
	assert saved.status == 200, (saved.status, saved.text[:300])
	uid = saved.json()["uid"]
	assert second_admin.request("DELETE", f"/grafana/api/dashboards/uid/{uid}").status == 200

	# the shipped dashboards are there - and can't be changed either
	shipped = second_admin.get("/grafana/api/search?type=dash-db&folderUIDs="
	                           "netrollout-operations").json()
	assert shipped, "no shipped dashboard in NetRollout/Operations"
	for found in shipped:
		answer = second_admin.request("DELETE", f"/grafana/api/dashboards/uid/{found['uid']}")
		assert answer.status == 403, (found["uid"], answer.status)


@pytest.mark.xfail(strict=True, reason=(
	"Known bug (found by these checks, 2026-10-10): the factory account is "
	"named admin, and so is Grafana's own server administrator - proxy auth "
	"signs it in as that one (isGrafanaAdmin, org role Admin), and Grafana's "
	"folder API grants it edit / delete on the shipped view-only folders"))
def test_the_factory_admin_is_an_editor_like_every_other_admin(install, admin):
	me = grafana_user(admin)
	assert me["login"] == "admin"
	assert me["isGrafanaAdmin"] is False
	assert [o["role"] for o in admin.get("/grafana/api/user/orgs").json()] == ["Editor"]
	assert admin.get("/grafana/api/folders/netrollout-operations").json()["canEdit"] is False
