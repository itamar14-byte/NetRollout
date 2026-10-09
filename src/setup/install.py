"""Installing: the questions - what the host knows (Facts), what the admin
decides (Answers), each answer's check and default, and asking (a question
with its default in [square brackets]; Enter accepts it; a wrong answer is
asked again with the reason) - then what installing writes into the install
folder: the folders, the certificate, site.env (the hostname for nginx and
the first start), and .env - last, because an existing .env means
"installed": nothing is ever overwritten."""
import datetime
import os
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, cast

from tzlocal.windows_tz import win_tz

from src import runtime
from src.access import certs, site_env
from src.db.settings import SETTINGS
from src.setup.env import DEV_COMPOSE, env_path, env_text, generate_secrets


# Suggested when 443 is taken, in this order
ALTERNATIVE_PORTS = (8443, 9443, 10443, 11443)
FALLBACK_HOSTNAME = "netrollout"


class Invalid(ValueError):
	"""An answer that can't be used; the message says why, for the admin."""


@dataclass
class Facts:
	"""What only the host can see, passed in by the script."""
	os: str = "linux"                       # windows | linux
	computer_name: str = ""
	timezone: str = ""                      # IANA or a Windows name
	server_ips: list[str] = field(default_factory=list)
	busy_ports: dict[int, str] = field(default_factory=dict)   # port → who
	account: str = ""                       # who runs the install


@dataclass
class Answers:
	"""What the admin decided (or the defaults)."""
	hostname: str
	https_port: int
	monitoring: bool
	org_certificate: bool
	timezone: str


# ── each answer's check ──

def check_hostname(value: str) -> str:
	""":returns: the hostname, normalised (lower case, no trailing dot)
	:raises Invalid: empty, or not a hostname (System Settings' rule)"""
	value = value.strip().rstrip(".").lower()
	if not value:
		raise Invalid("A hostname is needed (the certificate is made for it).")
	try:
		return cast(str, SETTINGS["public_hostname"].parse(value))
	except ValueError as e:
		raise Invalid(str(e)) from e


def check_port(value: str, busy: dict[int, str]) -> int:
	"""
	:param busy: the ports in use on this computer → who uses them
	:returns: the port
	:raises Invalid: not a port, in use, or 80 (the redirect's)"""
	try:
		port = cast(int, SETTINGS["https_port"].parse(value.strip()))
	except ValueError as e:
		raise Invalid(str(e)) from e
	if port in busy:
		raise Invalid(f"Port {port} is in use on this computer"
		              f"{f' (by {busy[port]})' if busy[port] else ''} - "
		              f"choose another, e.g. {free_port(busy)}.")
	if port == 80:
		raise Invalid("Port 80 is for the http -> https redirect - choose an "
		              "HTTPS port such as 443.")
	return port


def check_timezone(value: str) -> str:
	"""An IANA name (Asia/Jerusalem) as it is, else a Windows one (Israel
	Standard Time) -> its IANA name (CLDR's mapping, via tzlocal).

	:raises Invalid: neither"""
	value = value.strip()
	for name in (value, win_tz.get(value)):
		if name and _is_zone(name):
			return name
	raise Invalid(f"Unknown timezone '{value}' - e.g. Europe/London, "
	              f"Asia/Jerusalem, UTC.")


def _is_zone(name: str) -> bool:
	""":returns: whether this computer knows the IANA zone"""
	try:
		zoneinfo.ZoneInfo(name)
		return True
	except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
		# OSError: a name that isn't a path on this OS (e.g. "?" on Windows)
		return False


def check_yes_no(value: str) -> bool:
	""":raises Invalid: neither y / yes nor n / no"""
	value = value.strip().lower()
	if value in ("y", "yes"):
		return True
	if value in ("n", "no"):
		return False
	raise Invalid("Answer y or n.")


def check_org_certificate(hostname: str) -> None:
	"""The organisation's certificate is in the certs folder and usable.

	:raises Invalid: missing, or its problems (CertificateStore.check)"""
	store = certs.CertificateStore()
	try:
		check = store.check(hostname)
	except OSError:
		raise Invalid(f"Put the certificate ({certs.CERT_FILE}: yours first, "
		              f"then each issuer) and its key ({certs.KEY_FILE}, without "
		              f"a password) into {store.folder()}.") from None
	if not check.ok:
		raise Invalid(" ".join(check.problems))


# ── defaults ──

def free_port(busy: dict[int, str]) -> int:
	""":returns: 443, else the first free of ALTERNATIVE_PORTS (443 when all
	 are taken - the check then says so)"""
	return next((p for p in (443, *ALTERNATIVE_PORTS) if p not in busy), 443)


def default_hostname(facts: Facts) -> str:
	""":returns: the computer's name when it's a valid hostname, else
	 FALLBACK_HOSTNAME"""
	try:
		return check_hostname(facts.computer_name)
	except Invalid:
		return FALLBACK_HOSTNAME


def default_timezone(facts: Facts) -> str:
	""":returns: the computer's timezone (IANA), else UTC"""
	try:
		return check_timezone(facts.timezone) if facts.timezone else "UTC"
	except Invalid:
		return "UTC"


# ── asking ──

Read = Callable[[str], str]
Write = Callable[[str], None]
T = TypeVar("T")


class NoAnswer(Exception):
	"""Input ended (not a terminal) while a question waited."""


def ask(question: str, default: str, check: Callable[[str], T], read: Read,
        write: Write) -> T:
	"""`question [default]: ` until check() accepts the answer (Enter: the
	default); each refusal is written with its reason.

	:returns: what check() made of the answer
	:raises NoAnswer: input ended"""
	while True:
		try:
			raw = read(f"{question} [{default}]: ").strip()
		except EOFError:
			raise NoAnswer(question) from None
		try:
			return check(raw or default)
		except Invalid as e:
			write(f"  {e}")


def collect(facts: Facts, given: dict[str, str], interactive: bool,
            read: Read = input, write: Write = print) -> Answers:
	"""The answers: given (flags) are checked as they are; the rest asked,
	or — not interactive — their defaults.

	:param facts: what the host told (its name, timezone, busy ports)
	:param given: the answers passed as flags, by Answers' field names
	:param interactive: whether a person can be asked
	:raises Invalid: naming every bad given answer at once - or the
	 organisation's certificate isn't usable (not interactive)
	:raises NoAnswer: input ended during a question"""
	busy = facts.busy_ports
	questions: list[tuple[str, str, str, Callable[[str], Any]]] = [
		("hostname", "Hostname people will use", default_hostname(facts),
		 check_hostname),
		("https_port", "HTTPS port", str(free_port(busy)),
		 lambda v: check_port(v, busy)),
		("monitoring", "Monitoring (Prometheus, Loki, Grafana) y/n", "y",
		 check_yes_no),
		("org_certificate", "Use your organisation's certificate? y/n "
		 "(n: a self-signed one is made)", "n", check_yes_no),
		("timezone", "Timezone", default_timezone(facts), check_timezone),
	]
	values: dict[str, Any] = {}
	problems: list[str] = []
	for key, question, default, check in questions:
		if key in given:
			try:
				values[key] = check(given[key])
			except Invalid as e:
				problems.append(f"{key}: {e}")
		elif interactive:
			values[key] = ask(question, default, check, read, write)
		else:
			values[key] = check(default)
	if problems:
		raise Invalid("\n".join(problems))
	answers = Answers(**values)
	if answers.org_certificate:
		if interactive:
			write(f"Copy your certificate ({certs.CERT_FILE}) and key "
			      f"({certs.KEY_FILE}) into {runtime.certs_dir()}.")
			while True:
				try:
					read("Press Enter when they're there: ")
				except EOFError:
					raise NoAnswer("certificate") from None
				try:
					check_org_certificate(answers.hostname)
					break
				except Invalid as e:
					write(f"  {e}")
		else:
			check_org_certificate(answers.hostname)
	return answers


FOLDERS = ("logs", "config", "certs", "backups")


class Refused(Exception):
	"""Already installed (or set up): nothing was written."""


def install(answers: Answers, facts: Facts, now: datetime.datetime | None = None,
            version: str | None = None) -> list[str]:
	"""Write the install; returns what was done (one line each). Raises
	Refused when .env exists, OSError when a file can't be written (.env
	isn't written then, so the install can be run again)."""
	if env_path().exists():
		raise Refused(f"NetRollout is already installed here ({env_path()}).")
	done = []
	for name in FOLDERS:
		(runtime.home() / name).mkdir(parents=True, exist_ok=True)
	done.append("folders: " + ", ".join(FOLDERS))
	if answers.org_certificate:
		done.append("certificate: yours (checked)")
	else:
		certs.selfsigned(answers.hostname, facts.server_ips, runtime.certs_dir())
		done.append(f"certificate: self-signed for {answers.hostname}"
		            + (f" and {', '.join(facts.server_ips)}" if facts.server_ips else ""))
	site_env.update({site_env.HOSTNAME: answers.hostname,
	                 site_env.PORT_IN_USE: str(answers.https_port)})
	done.append(f"hostname: {answers.hostname}")
	text = env_text(answers, facts, generate_secrets(),
	                now or datetime.datetime.now(), version or runtime.VERSION,
	                port80_free=80 not in facts.busy_ports)
	_write_new(env_path(), text)
	done.append(f"settings: {env_path()}")
	return done


def install_dev(now: datetime.datetime | None = None) -> list[str]:
	"""A developer's setup in the repo: .env for the dev stack (built from the
	repo; the app runs on the host) and config/runtime.env pointing the host
	app at it, plus a self-signed certificate for localhost if there's none.
	Refuses when either file exists."""
	runtime_env = runtime.runtime_env()
	for path in (env_path(), runtime_env):
		if path.exists():
			raise Refused(f"{path} already exists - remove it to set up again.")
	keys = generate_secrets()
	done = []
	cert_dir = runtime.certs_dir()
	if not (cert_dir / certs.CERT_FILE).exists():
		certs.selfsigned("localhost", ["127.0.0.1"], cert_dir)
		done.append("certificate: self-signed for localhost and 127.0.0.1")
	now = now or datetime.datetime.now()
	lines = [f"# NetRollout development stack - written by `python -m src.setup "
	         f"init --dev` on {now:%Y-%m-%d %H:%M}.",
	         "# The dev stack's services; the app runs from the repo (config/runtime.env).",
	         "NETROLLOUT_VERSION=dev",
	         *(f"{k}={v}" for k, v in keys.items()),
	         "HTTPS_PORT=443", "TZ=UTC",
	         "COMPOSE_PROFILES=monitoring", "COMPOSE_PATH_SEPARATOR=,",
	         f"COMPOSE_FILE={DEV_COMPOSE}", ""]
	_write_new(env_path(), "\n".join(lines))
	done.append(f"stack settings: {env_path()}")
	# 127.0.0.1, not localhost: the stack publishes IPv4 only, and Windows
	# waits ~2 s on every refused ::1 attempt
	app = ["# The app on this computer -> the dev stack (127.0.0.1: IPv4 only).",
	       "PG_HOST=127.0.0.1", "PG_PORT=5432", "PG_NAME=netrollout",
	       "PG_USER=netrollout", f"PG_PASSWORD={keys['NETROLLOUT_DB_PASSWORD']}",
	       "DATABASE_URL=",
	       "REDIS_HOST=127.0.0.1", "REDIS_PORT=6379",
	       f"REDIS_PASSWORD={keys['REDIS_PASSWORD']}", "REDIS_URL=",
	       "# the app runs on the host, so compose can't pass this (the Grafana link)",
	       "NETROLLOUT_MONITORING=monitoring", ""]
	runtime_env.parent.mkdir(parents=True, exist_ok=True)
	_write_new(runtime_env, "\n".join(app))
	done.append(f"app settings: {runtime_env}")
	return done


def _write_new(path: Path, text: str) -> None:
	"""Create `path` (never replace one), readable by its owner only (on
	Windows the script restricts it)."""
	path.parent.mkdir(parents=True, exist_ok=True)
	with open(path, "x", encoding="utf-8", newline="\n") as f:
		f.write(text)
	try:
		os.chmod(path, 0o600)
	except OSError:
		pass
