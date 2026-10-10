"""Check 6: the first sign-in - the factory admin/admin is accepted, and
every page leads to the password change until a new password is set."""
from tests.e2e.harness import Browser, path_of

GATED = ("/dashboard", "/inventory", "/results", "/admin/users", "/admin/settings")


def test_admin_admin_signs_in_and_must_set_a_new_password_first(install, people):
	assert "admin" not in people, "the admin fixture ran first: run the module order"
	browser = Browser(install)
	answer = browser.sign_in("admin", "admin")
	assert answer.status == 302 and path_of(answer.location) == "/dashboard"
	for page in GATED:
		answer = browser.get(page)
		assert answer.status == 302, (page, answer.status)
		assert path_of(answer.location) == "/account/password", (page, answer.location)
	# Grafana: an admin, but not before the change
	assert browser.get("/grafana/").status == 403
	page = browser.get("/account/password")
	assert page.status == 200 and b"new_password" in page.body

	# the factory password can't stay; a weak one is refused
	assert path_of(browser.change_password("admin").location) == "/account/password"
	assert path_of(browser.change_password("short").location) == "/account/password"
	assert path_of(browser.get("/dashboard").location) == "/account/password"

	new = "Rollout-check-42"
	answer = browser.change_password(new)
	assert answer.status == 302 and path_of(answer.location) == "/dashboard"
	people["admin"] = new
	for page in GATED:
		assert browser.get(page).status == 200, page

	# the old password no longer works; the new one does
	assert path_of(Browser(install).sign_in("admin", "admin").location) == "/"
	again = Browser(install).sign_in("admin", new)
	assert again.status == 302 and path_of(again.location) == "/dashboard"
