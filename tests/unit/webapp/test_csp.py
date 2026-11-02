"""The Content-Security-Policy (src/webapp/hooks.py): scripts only from
NetRollout itself, no plugins, no framing by other sites, and the port
change's probe of this host on another port."""
from flask import Flask

from src.webapp.hooks import content_security_policy, register_security_headers


def directives(policy: str) -> dict[str, str]:
	return {d.split(" ", 1)[0]: d.split(" ", 1)[1] for d in (p.strip() for p in policy.split(";")) if d}


def test_scripts_come_only_from_netrollout():
	"""script-src is 'self' alone: no inline script, no eval, no CDN."""
	policy = directives(content_security_policy("netrollout.corp:443"))
	assert policy["script-src"] == "'self'"
	assert policy["default-src"] == "'self'" and policy["object-src"] == "'none'"
	assert policy["base-uri"] == "'self'" and policy["frame-ancestors"] == "'self'"
	assert policy["form-action"] == "'self'"
	assert policy["style-src"] == "'self' 'unsafe-inline'"
	assert policy["img-src"] == "'self' data:" and policy["font-src"] == "'self'"
	assert policy["report-uri"] == "/_netrollout/csp-report"


def test_the_port_probe_may_ask_this_host_on_any_port():
	"""connect-src names the page's own host on any port (System Settings
	probes the new HTTPS port) - never another host; an IPv6 address, which
	CSP can't name, gets 'self' only."""
	assert directives(content_security_policy("netrollout.corp:8443"))["connect-src"] == \
		"'self' https://netrollout.corp:*"
	assert directives(content_security_policy("192.0.2.5"))["connect-src"] == "'self' https://192.0.2.5:*"
	assert directives(content_security_policy("[2001:db8::5]:443"))["connect-src"] == "'self'"
	assert directives(content_security_policy("evil.test;script-src *"))["connect-src"] == "'self'"


def test_every_answer_carries_it():
	"""register_security_headers adds the header (or its report-only form) to
	every answer, a page's or a script's."""
	app = Flask(__name__)
	app.add_url_rule("/x", "x", lambda: "page")
	register_security_headers(app)
	resp = app.test_client().get("/x", headers={"Host": "nr.corp"})
	assert resp.headers["Content-Security-Policy"] == content_security_policy("nr.corp")
	app = Flask(__name__)
	app.add_url_rule("/x", "x", lambda: "page")
	register_security_headers(app, "Content-Security-Policy-Report-Only")
	resp = app.test_client().get("/x")
	assert "Content-Security-Policy-Report-Only" in resp.headers and "Content-Security-Policy" not in resp.headers
