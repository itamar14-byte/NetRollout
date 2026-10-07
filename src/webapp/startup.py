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
import secrets
import socket
import ssl
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.runtime import VERSION, in_container

INSTANCE_PATH = "/_netrollout/instance"
HEALTH_PATH = "/_netrollout/health"
# nginx's auth check for /grafana/ (deploy/nginx/site.conf.template)
GRAFANA_AUTH_PATH = "/_netrollout/grafana-auth"
PROBE_TIMEOUT = 2.0
READY_TIMEOUT = 60.0

OPEN_BROWSER_ENV = "NETROLLOUT_OPEN_BROWSER"
RELAUNCH_ENV = "NETROLLOUT_RELAUNCH"   # set by the admin Restart relaunch
# While no hostname is set: where nginx serves on this machine (dev, a fresh
# install) — the address the dev stack and the installer's certificate cover
DEFAULT_PUBLIC_URL = "https://localhost"
DEFAULT_SOURCE = "the default — no hostname set in System Settings"


def new_instance_token() -> str:
	return secrets.token_hex(16)


# ── Where the Public URL comes from ──────────────────────────────────────────

def resolve_public_url(setting: str | None = None) -> tuple[str, str]:
	"""(url, source). The URL built from System Settings (hostname + HTTPS
	port; install values are seeded into those rows) — or, while no hostname
	is set, https://localhost."""
	if setting:
		return setting.rstrip("/"), "from System Settings"
	return DEFAULT_PUBLIC_URL, DEFAULT_SOURCE


# ── Probing ──────────────────────────────────────────────────────────────────

@dataclass
class Probe:
	"""One check of an address: did NetRollout - this instance - answer?"""
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
		return (f"Available at {public_url}\n"
		        f"  (reverse proxy verified; Public URL {source})", public_url)
	if public_url and local and local.ok:
		return (f"nginx is up and forwarding to this instance, but "
		        f"{public_url} isn't reachable from this machine "
		        f"({public.reason if public else 'not checked'}).\n"
		        f"  If other computers can open it, you're fine; otherwise check "
		        f"DNS / firewall. (Public URL {source})\n{fallback}", direct)
	if public_url:
		why = public.reason if public else "not checked"
		if local and local.reason != why:
			why += f"; on this machine: {local.reason}"
		# a wrong port shows up as "couldn't connect"; nginx answering at all
		# (404, 502, …) means the port was right
		hint = ("\n  nginx may serve on another name or port — if so, set "
		        "the hostname and HTTPS port in System Settings."
		        if source == DEFAULT_SOURCE and public
		        and public.unreachable else "")
		return (f"Reverse proxy not verified at {public_url} "
		        f"({why}). (Public URL {source}){hint}\n{fallback}\n"
		        f"  Remote users can't sign in until the reverse proxy works.",
		        direct)
	return (f"No reverse proxy configured.\n{fallback}\n"
	        f"  Remote users need the reverse proxy (nginx) to sign in.",
	        direct)


# ── Opening the browser ──────────────────────────────────────────────────────

def should_open_browser(env: Mapping[str, str] = os.environ, platform: str = sys.platform,
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


def container_announcement(public_url: str | None) -> str:
	"""The startup line in a container. Nobody watches a container's console
	and the app can't reliably reach its own published port from inside, so
	nothing is probed here: the installer and `netrollout status` check the
	address from the host (HEALTH_PATH)."""
	if public_url:
		return (f"NetRollout {VERSION} started — expected at {public_url} "
		        f"(`netrollout status` checks it from the host)")
	return (f"NetRollout {VERSION} started — no hostname set in System "
	        f"Settings yet")


# ── Wiring ───────────────────────────────────────────────────────────────────

def _wait_until_serving(port: int, token: str, deadline: float) -> bool:
	""":returns: whether this instance answered on `port` before `deadline`
	 (time.monotonic())"""
	while time.monotonic() < deadline:
		if probe(f"http://127.0.0.1:{port}", token, timeout=1.0).ok:
			return True
		time.sleep(0.25)
	return False


def start_announcer(token: str, app_port: int,
                    public_setting: Callable[[], str | None] | None = None,
                    open_browser: bool | None = None) -> threading.Thread:
	"""Background thread: wait for Waitress, check the proxy, print where
	NetRollout is available, maybe open it.

	:param token: this run's instance token (the probe compares it)
	:param public_setting: returns the admin's public address (or None)
	:param open_browser: whether to open it; None: should_open_browser()
	:returns: the thread, started"""
	def run() -> None:
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
		print("[NetRollout] " + message, flush=True)
		if should_open_browser() if open_browser is None else open_browser:
			try:
				webbrowser.open(target)
			except Exception:
				pass
	thread = threading.Thread(target=run, name="startup-announcer",
	                          daemon=True)
	thread.start()
	return thread
