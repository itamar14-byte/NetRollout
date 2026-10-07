"""The install questions: what the host knows (Facts), what the admin
decides (Answers), each answer's check and default, and asking - a question
with its default in [square brackets]; Enter accepts it; a wrong answer is
asked again with the reason."""
import zoneinfo
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar, cast

from tzlocal.windows_tz import win_tz

from src import runtime
from src.access import certs
from src.db.settings import SETTINGS

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

	:raises Invalid: missing, or its problems (certs.validate)"""
	folder = runtime.certs_dir()
	try:
		cert = (folder / certs.CERT_FILE).read_bytes()
		key = (folder / certs.KEY_FILE).read_bytes()
	except OSError:
		raise Invalid(f"Put the certificate ({certs.CERT_FILE}: yours first, "
		              f"then each issuer) and its key ({certs.KEY_FILE}, without "
		              f"a password) into {folder}.") from None
	check = certs.validate(cert, key, hostname)
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
