"""Startup reverse-proxy check (src/webapp/startup.py): Public URL
precedence, the token probe against real local HTTP
and TLS listeners, the message variants, and the browser guards."""
import datetime
import http.server
import json
import ssl
import threading

import pytest

from src.webapp import startup
from src.webapp.startup import (Probe, announcement, probe,
                                resolve_public_url, should_open_browser)

TOKEN = "a" * 32


def test_public_url_precedence():
	# no hostname set: https://localhost
	assert resolve_public_url() == ("https://localhost", startup.DEFAULT_SOURCE)
	assert resolve_public_url(None) == ("https://localhost", startup.DEFAULT_SOURCE)
	# the URL built from System Settings wins
	assert resolve_public_url("https://admin.corp:8443/") == (
		"https://admin.corp:8443", "from System Settings")


# ── The probe, against real listeners ───────────────────────────────────────

def serve(handler_body, tls=False, tmp_path=None):
	"""A local server whose GET returns (status, headers, body) from
	handler_body(path, host_header). Returns (port, received_hosts, stop)."""
	hosts = []

	class Handler(http.server.BaseHTTPRequestHandler):
		def do_GET(self):
			hosts.append(self.headers.get("Host"))
			status, headers, body = handler_body(self.path)
			self.send_response(status)
			for k, v in headers.items():
				self.send_header(k, v)
			self.end_headers()
			self.wfile.write(body)

		def log_message(self, *_):
			pass

	server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
	if tls:
		cert, key = self_signed(tmp_path)
		ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
		ctx.load_cert_chain(cert, key)
		server.socket = ctx.wrap_socket(server.socket, server_side=True)
	threading.Thread(target=server.serve_forever, daemon=True).start()

	def stop():
		server.shutdown()
		server.server_close()   # release the port: later connects are refused
	return server.server_address[1], hosts, stop


def self_signed(tmp_path):
	from cryptography import x509
	from cryptography.hazmat.primitives import hashes, serialization
	from cryptography.hazmat.primitives.asymmetric import ec
	from cryptography.x509.oid import NameOID
	key = ec.generate_private_key(ec.SECP256R1())
	name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "netrollout.test")])
	now = datetime.datetime.now(datetime.timezone.utc)
	cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
	        .public_key(key.public_key()).serial_number(1)
	        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
	        .sign(key, hashes.SHA256()))
	cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
	cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
	key_path.write_bytes(key.private_bytes(
		serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
		serialization.NoEncryption()))
	return str(cert_path), str(key_path)


def json_body(token):
	return 200, {"Content-Type": "application/json"}, json.dumps(
		{"instance": token}).encode()


def test_probe_matches_our_token_over_self_signed_tls(tmp_path):
	port, hosts, stop = serve(lambda _: json_body(TOKEN), tls=True,
	                          tmp_path=tmp_path)
	try:
		# connect to loopback while presenting another hostname (SNI + Host)
		result = probe(f"https://netrollout.test:{port}", TOKEN,
		               connect_host="127.0.0.1")
	finally:
		stop()
	assert result == Probe(True, "")
	assert hosts == [f"netrollout.test:{port}"]


@pytest.mark.parametrize("response,reason", [
	(json_body("b" * 32), "it forwards to a different NetRollout instance"),
	((404, {}, b"not found"), "HTTP 404 — something other than NetRollout answers there"),
	((200, {}, b"<html>"), "something other than NetRollout answers there"),
	((502, {}, b"bad gateway"), "502 Bad Gateway — nginx is up but not forwarding to this app's port"),
	((301, {"Location": "https://x/"}, b""), "301 redirect to https://x/ — use that address as the Public URL"),
])
def test_probe_reports_why(response, reason):
	port, _, stop = serve(lambda _: response)
	try:
		result = probe(f"http://127.0.0.1:{port}", TOKEN)
	finally:
		stop()
	assert result == Probe(False, reason)


def test_probe_tls_against_plain_http_and_nothing_listening():
	port, _, stop = serve(lambda _: json_body(TOKEN))
	try:
		assert probe(f"https://127.0.0.1:{port}", TOKEN).reason.startswith(
			"TLS handshake failed")
	finally:
		stop()
	# the stopped server's port: nothing listens there any more
	result = probe(f"http://127.0.0.1:{port}", TOKEN, timeout=1)
	assert not result.ok and result.unreachable and str(port) in result.reason
	assert probe("not a url", TOKEN).reason == "not a valid URL: not a url"


# ── The message ─────────────────────────────────────────────────────────────

OK, FAIL = Probe(True, ""), Probe(False, "HTTP 404 — something else")


def test_message_when_verified():
	msg, target = announcement(8080, "https://nr.corp", "System Settings", OK, OK)
	assert msg.startswith("Available at https://nr.corp")
	assert target == "https://nr.corp"
	# nginx on another machine: the public step alone proves it
	msg, target = announcement(8080, "https://nr.corp", "System Settings",
	                           FAIL, OK)
	assert target == "https://nr.corp" and msg.startswith("Available at")


def test_message_when_only_this_machine_cant_reach_the_public_url():
	msg, target = announcement(8080, "https://nr.corp", "NETROLLOUT_PUBLIC_URL",
	                           OK, Probe(False, "timed out connecting to nr.corp:443"))
	assert "nginx is up and forwarding to this instance" in msg
	assert "isn't reachable from this machine" in msg
	assert target == "http://localhost:8080"


def test_message_when_not_verified():
	msg, target = announcement(9090, "https://localhost", startup.DEFAULT_SOURCE,
	                           Probe(False, "nothing listening on 127.0.0.1:443", True),
	                           Probe(False, "nothing listening on localhost:443", True))
	assert "Reverse proxy not verified at https://localhost" in msg
	assert "http://localhost:9090" in msg and "this machine only" in msg
	assert "Remote users can't sign in" in msg
	assert "set the hostname and HTTPS port" in msg   # the default's hint
	assert target == "http://localhost:9090"
	msg, _ = announcement(9090, None, "none configured", None, None)
	assert "No reverse proxy configured" in msg and "http://localhost:9090" in msg
	# nginx answered (wrong upstream), so the port was right: no port hint
	msg, _ = announcement(9090, "https://localhost", startup.DEFAULT_SOURCE,
	                      FAIL, FAIL)
	assert "set the hostname and HTTPS port" not in msg


# ── Browser guards ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,platform,container,expected", [
	({}, "win32", False, True),
	({"DISPLAY": ":0"}, "linux", False, True),
	({}, "linux", False, False),                           # headless server
	({}, "win32", True, False),                            # container
	({"NETROLLOUT_RELAUNCH": "1"}, "win32", False, False), # admin restart
	({"NETROLLOUT_OPEN_BROWSER": "0"}, "win32", False, False),
	({"NETROLLOUT_OPEN_BROWSER": "off"}, "darwin", False, False),
])
def test_should_open_browser(env, platform, container, expected):
	assert should_open_browser(env, platform, container) is expected


# ── End to end: the announcer thread ────────────────────────────────────────

def test_announcer_prints_the_verified_url(capsys, monkeypatch):
	port, _, stop = serve(lambda _: json_body(TOKEN))
	opened = []
	monkeypatch.setattr(startup.webbrowser, "open", opened.append)
	try:
		# the same local server plays both the app and the proxy here
		startup.start_announcer(TOKEN, port,
		                        public_setting=lambda: f"http://127.0.0.1:{port}",
		                        open_browser=True).join(timeout=15)
	finally:
		stop()
	out = capsys.readouterr().out
	# one line per message: no blank lines around it, name not repeated
	assert out.splitlines()[0] == f"[NetRollout] Available at http://127.0.0.1:{port}"
	assert opened == [f"http://127.0.0.1:{port}"]
