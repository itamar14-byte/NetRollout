"""Authorization floor for EVERY route, derived from the live url_map.

New routes are covered automatically: unauthenticated requests must be
refused (redirect to login), and non-admins must be refused on /admin/*.
Admin routes are only ever called as non-admins here — they are refused
before their body runs, so /admin/server/restart (os._exit) and the config
save routes can't fire.
"""
import re
import uuid

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

PUBLIC_ENDPOINTS = {
	"static", "auth.home", "auth.login_get", "auth.login", "auth.register_form",
	"auth.register", "auth.otp_enroll", "auth.otp_verify", "auth.logout",
	"prometheus_metrics",
}
SKIP_PREFIXES = ("/_test/", "/rollout/stream/_test/", "/static/")


def _url(rule) -> str:
	def fill(m):
		converter = m.group(1) or "default"
		return str(uuid.uuid4()) if converter == "uuid" else "x"
	return re.sub(r"<(?:(\w+):)?\w+>", fill, rule.rule)


def _routes(app):
	for rule in app.url_map.iter_rules():
		if rule.rule.startswith(SKIP_PREFIXES):
			continue
		for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
			yield rule, method


def test_route_inventory_is_complete(app):
	# Guard for the matrix itself: all blueprint routes are discovered.
	# 74 @bp.route rules -> 73 endpoints (properties create/quick_create share
	# one). A floor, so adding routes never breaks this.
	endpoints = {r.endpoint for r, _ in _routes(app)}
	assert len({e for e in endpoints if "." in e}) >= 73, sorted(endpoints)


def test_unauthenticated_requests_are_refused(app, client_for):
	client = client_for()
	failures = []
	for rule, method in _routes(app):
		if rule.endpoint in PUBLIC_ENDPOINTS:
			continue
		resp = client.open(_url(rule), method=method)
		location = resp.headers.get("Location", "")
		if not (resp.status_code == 302 and location.startswith("/?next=")):
			failures.append(f"{method} {rule.rule} -> {resp.status_code} {location}")
	assert not failures, "\n".join(failures)


def test_non_admins_are_refused_on_admin_routes(app, client_for, make_user):
	user = make_user(role="user")
	page, xhr = client_for(user), client_for(user, xhr=True)
	failures = []
	for rule, method in _routes(app):
		if not rule.rule.startswith("/admin"):
			continue
		resp = page.open(_url(rule), method=method)
		if not (resp.status_code == 302 and
		        resp.headers["Location"].endswith("/dashboard")):
			failures.append(f"page {method} {rule.rule} -> {resp.status_code}")
		resp = xhr.open(_url(rule), method=method)
		if resp.status_code != 403:
			failures.append(f"xhr  {method} {rule.rule} -> {resp.status_code}")
	assert not failures, "\n".join(failures)


def test_public_pages_render(client_for):
	client = client_for()
	for path in ("/", "/register"):
		assert client.get(path).status_code == 200, path


def test_logout_when_not_logged_in_redirects_home(client_for):
	resp = client_for().get("/logout")
	assert resp.status_code == 302 and resp.headers["Location"] == "/"
