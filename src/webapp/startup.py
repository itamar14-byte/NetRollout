"""Startup check: is the reverse proxy up and forwarding to *this* instance?

Once Waitress answers, a background thread checks the Public URL in two
steps and prints the address people should use:

1. local proxy — nginx on this machine (loopback, the Public URL's port, its
   hostname as SNI and Host) returns our per-run instance token;
2. public URL  — the token comes back through the Public URL itself.

The token is what proves identity: an unrelated nginx, or nginx forwarding
to another NetRollout, can't return it. Certificates aren't verified (this
checks identity, not trust). Requests use http.client, which never goes
through an HTTP proxy — a corporate proxy would otherwise answer for us.
"""
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

INSTANCE_PATH = "/_netrollout/instance"
PROBE_TIMEOUT = 2.0
READY_TIMEOUT = 60.0

PUBLIC_URL_ENV = "NETROLLOUT_PUBLIC_URL"
NGINX_CONF_ENV = "NETROLLOUT_NGINX_CONF"
OPEN_BROWSER_ENV = "NETROLLOUT_OPEN_BROWSER"
RELAUNCH_ENV = "NETROLLOUT_RELAUNCH"   # set by the admin Restart relaunch
DEFAULT_NGINX_CONF = Path(__file__).resolve().parents[2] / "docs" / "nginx" \
	/ "nginx.conf"


def new_instance_token() -> str:
	return secrets.token_hex(16)


# ── Where the Public URL comes from ──────────────────────────────────────────

def detect_from_nginx_conf(path: Path) -> str | None:
	"""Public URL from an nginx config: the first server block that listens
	with ssl (else plain http), and its server_name (`_` or none → localhost).
	The port is nginx's own — behind a Docker port mapping the host port can
	differ, which is why an explicit Public URL wins over this."""
	try:
		text = path.read_text(encoding="utf-8")
	except OSError:
		return None
	text = re.sub(r"#[^\n]*", "", text)
	candidates = []
	for block in _server_blocks(text):
		names = re.findall(r"\bserver_name\s+([^;]+);", block)
		name = next((n for n in (names[0].split() if names else [])
		             if n not in ("_", "\"\"")), "localhost")
		for listen in re.findall(r"\blisten\s+([^;]+);", block):
			parts = listen.split()
			port_match = re.search(r"(\d+)$", parts[0])
			if not port_match:
				continue
			port = int(port_match.group(1))
			tls = "ssl" in parts[1:]
			candidates.append((tls, name, port))
	if not candidates:
		return None
	tls, name, port = sorted(candidates, key=lambda c: not c[0])[0]
	scheme = "https" if tls else "http"
	default = 443 if tls else 80
	return f"{scheme}://{name}" + ("" if port == default else f":{port}")


def _server_blocks(text: str) -> list[str]:
	"""Bodies of `server { … }` blocks (brace-matched)."""
	blocks = []
	for m in re.finditer(r"\bserver\s*\{", text):
		depth, i = 1, m.end()
		while i < len(text) and depth:
			depth += {"{": 1, "}": -1}.get(text[i], 0)
			i += 1
		blocks.append(text[m.end():i - 1])
	return blocks


def resolve_public_url(setting: str | None = None) -> tuple[str | None, str]:
	"""(url, source). Admin setting > install-time env > nginx config."""
	if setting:
		return setting.rstrip("/"), "System Settings"
	if os.environ.get(PUBLIC_URL_ENV):
		return os.environ[PUBLIC_URL_ENV].rstrip("/"), f"{PUBLIC_URL_ENV}"
	conf = Path(os.environ.get(NGINX_CONF_ENV) or DEFAULT_NGINX_CONF)
	detected = detect_from_nginx_conf(conf)
	if detected:
		return detected, f"auto-detected from {conf.name}"
	return None, "none configured"


# ── Probing ──────────────────────────────────────────────────────────────────

@dataclass
class Probe:
	ok: bool
	reason: str                  # human-readable; "" when ok
	unreachable: bool = False    # couldn't connect at all (wrong host/port?)


def probe(url: str, token: str, connect_host: str | None = None,
          timeout: float = PROBE_TIMEOUT) -> Probe:
	"""Fetch INSTANCE_PATH through `url` and compare the token.
	connect_host: open the TCP connection there (e.g. 127.0.0.1) while still
	presenting the URL's hostname as SNI and Host — so a multi-vhost nginx
	routes it to the right server block."""
	parts = urlsplit(url)
	if parts.scheme not in ("http", "https") or not parts.hostname:
		return Probe(False, f"not a valid URL: {url}")
	tls = parts.scheme == "https"
	port = parts.port or (443 if tls else 80)
	host = parts.hostname
	netloc_host = host if parts.port is None else f"{host}:{parts.port}"
	try:
		sock = socket.create_connection((connect_host or host, port),
		                                timeout=timeout)
	except socket.timeout:
		return Probe(False, f"timed out connecting to {connect_host or host}:{port}",
		             unreachable=True)
	except ConnectionRefusedError:
		return Probe(False, f"nothing listening on {connect_host or host}:{port}",
		             unreachable=True)
	except OSError as e:
		return Probe(False, f"can't connect to {connect_host or host}:{port} "
		                    f"({e.strerror or e})", unreachable=True)
	try:
		if tls:
			ctx = ssl.create_default_context()
			ctx.check_hostname = False
			ctx.verify_mode = ssl.CERT_NONE
			try:
				sock = ctx.wrap_socket(sock, server_hostname=host)
			except (ssl.SSLError, OSError) as e:
				sock.close()
				return Probe(False, f"TLS handshake failed ({e.__class__.__name__})")
		conn = http.client.HTTPConnection(netloc_host, timeout=timeout)
		conn.sock = sock   # reuse our (possibly TLS) socket; no proxy involved
		conn.request("GET", INSTANCE_PATH,
		             headers={"Host": netloc_host, "Accept": "application/json"})
		resp = conn.getresponse()
		body = resp.read(4096)
		conn.close()
	except socket.timeout:
		return Probe(False, "timed out waiting for a response")
	except (OSError, http.client.HTTPException) as e:
		return Probe(False, f"request failed ({e.__class__.__name__})")
	if resp.status == 502:
		return Probe(False, "502 Bad Gateway — nginx is up but not forwarding "
		                    "to this app's port")
	if resp.status in (301, 302, 307, 308):
		return Probe(False, f"{resp.status} redirect to "
		                    f"{resp.getheader('Location', '?')} — use that "
		                    f"address as the Public URL")
	if resp.status != 200:
		return Probe(False, f"HTTP {resp.status} — something other than "
		                    f"NetRollout answers there")
	try:
		theirs = str(json.loads(body).get("instance", ""))
	except (ValueError, AttributeError):
		return Probe(False, "something other than NetRollout answers there")
	if not secrets.compare_digest(theirs, token):
		return Probe(False, "it forwards to a different NetRollout instance")
	return Probe(True, "")


def check_proxy(public_url: str, token: str,
                timeout: float = PROBE_TIMEOUT) -> tuple[Probe, Probe]:
	"""(local, public). The local step connects to loopback."""
	local = probe(public_url, token, connect_host="127.0.0.1", timeout=timeout)
	public = probe(public_url, token, timeout=timeout)
	return local, public


# ── The message ──────────────────────────────────────────────────────────────

def announcement(app_port: int, public_url: str | None, source: str,
                 local: Probe | None, public: Probe | None) -> tuple[str, str]:
	"""(message, url to open)."""
	direct = f"http://localhost:{app_port}"
	fallback = (f"  Local fallback: {direct} — sign-in works from this machine "
	            f"only (the session cookie is HTTPS-only).")
	# the public step alone proves it — nginx may live on another machine
	if public_url and public and public.ok:
		return (f"NetRollout is available at {public_url}\n"
		        f"  (reverse proxy verified; Public URL {source})", public_url)
	if public_url and local and local.ok:
		return (f"nginx is up and forwarding to this instance, but "
		        f"{public_url} isn't reachable from this machine ({public.reason}).\n"
		        f"  If other computers can open it, you're fine; otherwise check "
		        f"DNS / firewall. (Public URL {source})\n{fallback}", direct)
	if public_url:
		why = public.reason if public else "not checked"
		if local and local.reason != why:
			why += f"; on this machine: {local.reason}"
		# a wrong port shows up as "couldn't connect"; nginx answering at all
		# (404, 502, …) means the port was right
		hint = ("\n  The port in the nginx config can differ from the host "
		        "port behind a Docker mapping — set the Public URL if so."
		        if source.startswith("auto-detected") and public
		        and public.unreachable else "")
		return (f"Reverse proxy not verified at {public_url} "
		        f"({why}). (Public URL {source}){hint}\n{fallback}\n"
		        f"  Remote users can't sign in until the reverse proxy works.",
		        direct)
	return (f"No reverse proxy configured.\n{fallback}\n"
	        f"  Remote users need the reverse proxy (nginx) to sign in.",
	        direct)


# ── Opening the browser ──────────────────────────────────────────────────────

def in_container() -> bool:
	return Path("/.dockerenv").exists() or bool(os.environ.get("container"))


def should_open_browser(env=os.environ, platform: str = sys.platform,
                        container: bool | None = None) -> bool:
	"""Open on a normal launch at a desktop. Not in a container, not on an
	admin-restart relaunch, not when opted out, not on a Linux machine with
	no display (a server — webbrowser would fall back to a text browser)."""
	if env.get(OPEN_BROWSER_ENV, "1").strip().lower() in ("0", "false", "no",
	                                                      "off"):
		return False
	if env.get(RELAUNCH_ENV):
		return False
	if in_container() if container is None else container:
		return False
	if platform.startswith("linux") and not (env.get("DISPLAY") or
	                                         env.get("WAYLAND_DISPLAY")):
		return False
	return True


# ── Wiring ───────────────────────────────────────────────────────────────────

def _wait_until_serving(port: int, token: str, deadline: float) -> bool:
	while time.monotonic() < deadline:
		if probe(f"http://127.0.0.1:{port}", token, timeout=1.0).ok:
			return True
		time.sleep(0.25)
	return False


def start_announcer(token: str, app_port: int, public_setting=None,
                    open_browser: bool | None = None) -> threading.Thread:
	"""Background thread: wait for Waitress, check the proxy, print where
	NetRollout is available, maybe open it. public_setting: a callable
	returning the admin's Public URL setting (or None)."""
	def run():
		if not _wait_until_serving(app_port, token,
		                           time.monotonic() + READY_TIMEOUT):
			print(f"[NetRollout] The app didn't answer on port {app_port} "
			      f"within {int(READY_TIMEOUT)}s.", flush=True)
			return
		try:
			setting = public_setting() if public_setting else None
		except Exception:   # settings unavailable (e.g. DB down) — use the rest
			setting = None
		url, source = resolve_public_url(setting)
		local = public = None
		if url:
			local, public = check_proxy(url, token)
		message, target = announcement(app_port, url, source, local, public)
		print("\n[NetRollout] " + message + "\n", flush=True)
		if should_open_browser() if open_browser is None else open_browser:
			try:
				webbrowser.open(target)
			except Exception:
				pass
	thread = threading.Thread(target=run, name="startup-announcer",
	                          daemon=True)
	thread.start()
	return thread
