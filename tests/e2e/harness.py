"""The end-to-end checks' tools: a scratch install made from the release's
own files and run like one (ScratchInstall), and a browser that talks to it
through nginx with the standard library only (Browser).

Everything runs under the compose project PROJECT on ports HTTPS_PORT /
HTTP_PORT with the images tagged TAG - never the developer's install or dev
stack."""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

PROJECT = "nre2e"
HOSTNAME = "e2e.netrollout.test"
HTTPS_PORT = 18443
HTTP_PORT = 18080
TAG = os.environ.get("NETROLLOUT_E2E_TAG", "e2e-local")
APP_IMAGE = f"itamarweinstein/netrollout:{TAG}"
NGINX_IMAGE = f"itamarweinstein/netrollout-nginx:{TAG}"
APP_UID = 10001   # the app container's user (packaging/linux/netrollout.sh)

# compose.http.yaml publishes port 80, which this computer may use (the dev
# stack does): the install gets a copy of it publishing HTTP_PORT instead
HTTP_COMPOSE = "compose.e2e-http.yaml"
# Every service of an install with monitoring on
SERVICES = {"postgres", "redis", "app", "nginx", "prometheus", "loki", "alloy",
            "grafana", "grafana-setup"}
# What the host scripts' compose calls must not inherit from this shell
FOREIGN_ENV = ("COMPOSE_", "NETROLLOUT_")


def shipped() -> dict[str, str]:
	""":returns: SHIPPED of packaging/build_release.py (install path -> repo
	 path), loaded as tests/unit/packaging/test_build_release.py loads it"""
	spec = importlib.util.spec_from_file_location(
		"build_release", ROOT / "packaging" / "build_release.py")
	assert spec and spec.loader
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return dict(module.SHIPPED)


def run(*args: str, input: bytes | None = None, check: bool = True,
        timeout: float = 300, cwd: Path | None = None) -> subprocess.CompletedProcess:
	"""A command, its output captured as bytes (never text: a restored file
	must come back byte for byte); the environment without COMPOSE_* /
	NETROLLOUT_* and with NETROLLOUT_VERSION = TAG, as the scripts run compose.

	:raises AssertionError: check and it failed (its output in the message)"""
	env = {k: v for k, v in os.environ.items() if not k.startswith(FOREIGN_ENV)}
	env["NETROLLOUT_VERSION"] = TAG
	done = subprocess.run(list(args), input=input, capture_output=True,
	                      timeout=timeout, cwd=cwd, env=env)
	if check and done.returncode != 0:
		raise AssertionError(
			f"{' '.join(args)} -> exit {done.returncode}\n"
			f"{done.stdout.decode(errors='replace')}\n"
			f"{done.stderr.decode(errors='replace')}")
	return done


def image_exists(image: str) -> bool:
	return run("docker", "image", "inspect", image, check=False).returncode == 0


def wait_for(check: Callable[[], Any], timeout: float, what: str,
             every: float = 1.0) -> Any:
	"""Poll until `check` returns something truthy (an exception counts as
	not yet).

	:returns: what it returned
	:raises AssertionError: not within `timeout` seconds - says what was
	 awaited and the last answer"""
	deadline = time.monotonic() + timeout
	last: Any = None
	while True:
		try:
			last = check()
			if last:
				return last
		except Exception as e:   # not up yet: the next poll decides
			last = e
		if time.monotonic() > deadline:
			raise AssertionError(f"Waited {timeout:.0f} s for {what}; last: {last!r}")
		time.sleep(every)


class ScratchInstall:
	"""An install in `folder`: the release's files, the setup core's answers,
	the compose project PROJECT."""

	def __init__(self, folder: Path) -> None:
		self.folder = folder
		self.certificate = folder / ".e2e" / "fullchain.pem"   # the CA to trust

	def is_scratch(self) -> bool:
		""":returns: the folder is missing, empty, or one these checks made -
		 the only kinds they may clear (never a real install by mistake)"""
		return not self.folder.exists() or not any(self.folder.iterdir()) 			or self.certificate.parent.is_dir()

	# ── making it ──

	def create(self) -> None:
		"""The files an install gets (SHIPPED + VERSION, LF as in the release
		zip), then the setup core's init in the app image as the Linux
		script's do_install runs it, prepare-start as its do_start does, the
		folders' owners as its set_owners sets them (Linux), and port 80
		swapped for HTTP_PORT."""
		self.folder.mkdir(parents=True, exist_ok=True)
		self.certificate.parent.mkdir(exist_ok=True)   # also the mark: ours (is_scratch)
		for target, source in shipped().items():
			path = self.folder / target
			path.parent.mkdir(parents=True, exist_ok=True)
			path.write_bytes((ROOT / source).read_bytes().replace(b"\r\n", b"\n"))
			if path.suffix == ".sh":
				path.chmod(0o755)
		(self.folder / "VERSION").write_bytes(f"{VERSION}\n".encode())
		http_compose = (ROOT / "compose.http.yaml").read_text(encoding="utf-8")
		assert '"80:80"' in http_compose, "compose.http.yaml no longer publishes 80:80"
		(self.folder / HTTP_COMPOSE).write_bytes(
			http_compose.replace('"80:80"', f'"{HTTP_PORT}:80"').encode())
		facts = ["--os", "linux", "--computer-name", "e2e", "--host-timezone", "UTC",
		         "--server-ips", "127.0.0.1", "--account", "e2e",
		         # port 80 stays this computer's: no compose.http.yaml
		         "--busy-ports", "80=the e2e checks"]
		self.setup_core("init", "--licence-accepted", "--defaults", *facts,
		                "--hostname", HOSTNAME, "--https-port", str(HTTPS_PORT),
		                "--monitoring", "y", "--org-certificate", "n",
		                "--timezone", "UTC")
		self.setup_core("prepare-start", *facts)
		if os.name == "posix":
			# set_owners: the app container (uid 10001) writes these; .env -
			# root's 600 there (sudo) - is the runner's here, as compose runs as
			# the runner
			self.as_root(f"chown -R {APP_UID}:{APP_UID} /install/config /install/certs "
			             f"/install/logs /install/backups && chmod 700 /install/backups && "
			             f"chown {os.getuid()}:{os.getgid()} /install/.env && "
			             f"chmod 600 /install/.env")
		env_file = self.folder / ".env"
		text = env_file.read_bytes().decode()
		assert "\nCOMPOSE_FILE=compose.yaml\n" in text, text
		env_file.write_bytes(text.replace(
			"\nCOMPOSE_FILE=compose.yaml\n",
			f"\nCOMPOSE_FILE=compose.yaml,{HTTP_COMPOSE}\n").encode())

	def setup_core(self, *args: str) -> str:
		""":returns: what `python -m src.setup <args>` printed, run as this
		 platform's script runs it (the folder at /install): as root on Linux
		 (netrollout.sh - the owners are set afterwards); as the image's user
		 on Windows (manage.ps1 - Docker Desktop's mounts keep the owner that
		 wrote a file, and the app must write into config/, certs/, logs/)"""
		user = ["--user", "0:0"] if os.name == "posix" else []
		done = run("docker", "run", "--rm", *user,
		           "-v", f"{self.folder}:/install", "-e", "NETROLLOUT_HOME=/install",
		           APP_IMAGE, "python", "-m", "src.setup", *args)
		return done.stdout.decode()

	def as_root(self, script: str) -> None:
		"""A shell script as root over the folder (/install): what the
		scripts do with sudo."""
		run("docker", "run", "--rm", "--user", "0:0", "-v", f"{self.folder}:/install",
		    APP_IMAGE, "sh", "-c", script)

	def env(self) -> dict[str, str]:
		""":returns: .env's keys (the last value wins)"""
		values = {}
		for line in (self.folder / ".env").read_text(encoding="utf-8").splitlines():
			key, sep, value = line.partition("=")
			if sep and not key.startswith("#"):
				values[key.strip()] = value.strip()
		return values

	# ── running it ──

	def compose(self, *args: str, input: bytes | None = None, check: bool = True,
	            timeout: float = 300) -> subprocess.CompletedProcess:
		"""docker compose from the install folder (compose reads .env's
		COMPOSE_FILE relative to the folder it runs in), as the scripts run it."""
		return run("docker", "compose", "-p", PROJECT, "--project-directory",
		           str(self.folder), "--env-file", str(self.folder / ".env"), *args,
		           input=input, check=check, timeout=timeout, cwd=self.folder)

	def up(self) -> None:
		"""Started from nothing (a volume left by an earlier run would keep
		that run's passwords), then waited for: every service healthy."""
		self.compose("down", "-v", "--remove-orphans")
		self.compose("up", "-d", "--wait", "--wait-timeout", "600", timeout=900)
		self.certificate.write_bytes(self.read("/data/certs/fullchain.pem"))

	def down(self) -> None:
		"""Stopped, its volumes removed (nothing to do before it was made)."""
		if (self.folder / ".env").exists():
			self.compose("down", "-v", "--remove-orphans", check=False)

	def remove(self) -> None:
		"""The folder, whatever owns its files (root's / the app's on Linux)."""
		if os.name == "posix" and self.folder.exists():
			self.as_root("rm -rf /install/* /install/.[!.]* || true")
		shutil.rmtree(self.folder, ignore_errors=True)

	def services(self) -> list[dict[str, Any]]:
		""":returns: `compose ps --all`'s rows (one JSON object per line, or
		 one array: either compose)"""
		out = self.compose("ps", "--all", "--format", "json").stdout.decode().strip()
		if out.startswith("["):
			return list(json.loads(out))
		return [json.loads(line) for line in out.splitlines() if line.strip()]

	def exec(self, service: str, *command: str, input: bytes | None = None,
	         check: bool = True) -> subprocess.CompletedProcess:
		return self.compose("exec", "-T", service, *command, input=input,
		                    check=check, timeout=120)

	def read(self, path: str) -> bytes:
		""":returns: a file of the app container (/data = the folder's
		 config/, certs/, ...), byte for byte"""
		return self.exec("app", "cat", path).stdout

	def write(self, path: str, data: bytes) -> None:
		"""Replace a file of the app container (as the app's user) at once:
		written beside it with its mode, then moved over it - no reader sees
		half of it."""
		self.exec("app", "sh", "-c",
		          'mode=$(stat -c %a "$1" 2>/dev/null || echo 644); '
		          'cat > "$1.e2e" && chmod "$mode" "$1.e2e" && mv "$1.e2e" "$1"',
		          "sh", path, input=data)

	def python(self, code: str, *args: str) -> str:
		""":returns: what Python printed in the app container (its env: the
		 encryption key, the clock the app uses)"""
		return self.exec("app", "python", "-c", code, *args).stdout.decode().strip()

	def sql(self, query: str, user: str = "postgres", password: str = "",
	        check: bool = True) -> subprocess.CompletedProcess:
		"""psql in the postgres container, unaligned, tuples only; as another
		login over TCP when a password is given."""
		login = ["-h", "127.0.0.1"] if password else []
		return self.exec("postgres", "env", f"PGPASSWORD={password}", "psql", *login,
		                 "-U", user, "-d", "netrollout", "-v", "ON_ERROR_STOP=1",
		                 "-tAc", query, check=check)

	def nginx_status(self) -> dict[str, str]:
		""":returns: nginx's last verdict (config/nginx/status.json)"""
		return dict(json.loads(self.read("/data/config/nginx/status.json")))

	# ── people ──

	def give_authenticator(self, username: str) -> str:
		"""The user's 2FA set up as an enrolment would (the secret stored
		encrypted with the install's key), so the checks can sign in.

		:returns: the secret (codes: totp())"""
		secret = self.python("import pyotp; print(pyotp.random_base32())")
		stored = self.python(
			"import sys; from src.encryption import encrypt, init_encryption; "
			"init_encryption(None); print(encrypt(sys.argv[1]))", secret)
		assert re.fullmatch(r"[A-Za-z0-9_=-]+", stored), stored
		self.sql(f"UPDATE users SET otp_secret = '{stored}' WHERE username = '{username}'")
		return secret

	def totp(self, secret: str) -> str:
		""":returns: the current code - by the containers' clock (Docker
		 Desktop's VM clock has lagged the host's by minutes)"""
		return self.python("import pyotp, sys; print(pyotp.TOTP(sys.argv[1]).now())", secret)


@dataclass
class Answer:
	status: int
	headers: dict[str, str]   # lower-case names
	body: bytes

	@property
	def location(self) -> str:
		return self.headers.get("location", "")

	def json(self) -> Any:
		return json.loads(self.body)

	@property
	def text(self) -> str:
		return self.body.decode(errors="replace")


CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"|CSRF_TOKEN = "([^"]+)"')


class Browser:
	"""A browser's requests to the install through nginx: TLS checked against
	the install's certificate (connecting to 127.0.0.1, which it covers), one
	Host header, cookies kept, redirects not followed (the checks look at
	them)."""

	def __init__(self, install: ScratchInstall, host: str = f"{HOSTNAME}:{HTTPS_PORT}") -> None:
		self.host = host
		self.cookies: dict[str, str] = {}
		self.context = ssl.create_default_context(cafile=str(install.certificate))

	def request(self, method: str, path: str, body: bytes | None = None,
	            headers: dict[str, str] | None = None) -> Answer:
		conn = http.client.HTTPSConnection("127.0.0.1", HTTPS_PORT,
		                                   context=self.context, timeout=60)
		sent = {"Host": self.host, "User-Agent": "NetRollout-e2e"}
		if self.cookies:
			sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
		if method != "GET":   # what a browser sends with a form or a fetch
			sent["Origin"] = f"https://{self.host}"
			sent["Referer"] = f"https://{self.host}/"
		sent.update(headers or {})
		try:
			conn.request(method, path, body=body, headers=sent)
			response = conn.getresponse()
			data = response.read()
		finally:
			conn.close()
		for cookie in response.msg.get_all("Set-Cookie") or []:
			name, _, rest = cookie.partition("=")
			value, _, attributes = rest.partition(";")
			if value in ("", '""') or "max-age=0" in attributes.lower() or \
			        "expires=thu, 01 jan 1970" in attributes.lower():
				self.cookies.pop(name.strip(), None)
			else:
				self.cookies[name.strip()] = value
		return Answer(response.status,
		              {k.lower(): v for k, v in response.getheaders()}, data)

	def get(self, path: str, headers: dict[str, str] | None = None) -> Answer:
		return self.request("GET", path, headers=headers)

	def post_form(self, path: str, fields: dict[str, str]) -> Answer:
		return self.request("POST", path, urllib.parse.urlencode(fields).encode(),
		                    {"Content-Type": "application/x-www-form-urlencoded"})

	def post_json(self, path: str, data: Any, csrf: str = "",
	              headers: dict[str, str] | None = None) -> Answer:
		sent = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
		if csrf:
			sent["X-CSRFToken"] = csrf
		sent.update(headers or {})
		return self.request("POST", path, json.dumps(data).encode(), sent)

	def csrf(self, page: str) -> str:
		""":returns: the CSRF token of a page (its form's, or its scripts')"""
		answer = self.get(page)
		assert answer.status == 200, f"{page}: {answer.status} {answer.location}"
		found = CSRF_RE.search(answer.text)
		assert found, f"no CSRF token on {page}"
		return found.group(1) or found.group(2)

	# ── signing in ──

	def sign_in(self, username: str, password: str) -> Answer:
		self.get("/")   # the sign-in page: the session starts there
		return self.post_form("/login", {"username": username, "password": password})

	def sign_in_with_code(self, install: ScratchInstall, username: str,
	                      password: str, secret: str) -> Answer:
		"""A local user's sign-in: the password, then the authenticator's code.

		:returns: the answer to the code"""
		answer = self.sign_in(username, password)
		assert answer.status == 302 and path_of(answer.location) == "/otp_verify", \
			f"{username}: {answer.status} {answer.location}"
		token = self.csrf("/otp_verify")
		return self.post_form("/otp_verify", {"code": install.totp(secret),
		                                      "csrf_token": token})

	def change_password(self, new: str, current: str = "") -> Answer:
		token = self.csrf("/account/password")
		fields = {"new_password": new, "confirm_password": new, "csrf_token": token}
		if current:
			fields["current_password"] = current
		return self.post_form("/account/password", fields)


def path_of(location: str) -> str:
	""":returns: a redirect's path (without the query)"""
	return urllib.parse.urlsplit(location).path


def http_get(host: str, path: str = "/") -> Answer:
	"""A plain-HTTP request to HTTP_PORT (port 80's redirect)."""
	conn = http.client.HTTPConnection("127.0.0.1", HTTP_PORT, timeout=30)
	try:
		conn.request("GET", path, headers={"Host": host})
		response = conn.getresponse()
		return Answer(response.status, {k.lower(): v for k, v in response.getheaders()},
		              response.read())
	finally:
		conn.close()
