"""The install questions: what the host knows (Facts), what the admin
decides (Answers), each answer's check and default, and asking - a question
with its default in [square brackets]; Enter accepts it; a wrong answer is
asked again with the reason."""
import zoneinfo
from dataclasses import dataclass, field
from typing import Callable

from tzlocal.windows_tz import win_tz

from src import certs, runtime
from src.db.settings import SETTINGS

# Suggested when 443 is taken, in this order
ALTERNATIVE_PORTS = (8443, 9443, 10443, 11443)
FALLBACK_HOSTNAME = "netrollout"
LICENCE = {
	"common": (
		"NetRollout is free software under the GNU Affero General Public "
		"License v3 (https://www.gnu.org/licenses/agpl-3.0.html): you may use, "
		"change and share it; if you offer a changed version to others over a "
		"network, you must offer them its source too. It comes with no "
		"warranty."),
	"windows": (
		"NetRollout runs on Docker Desktop. Docker Desktop is free for "
		"personal use, education, non-commercial open source and small "
		"businesses (fewer than 250 employees and less than $10 million in "
		"annual revenue); larger organisations need a paid Docker subscription "
		"(https://www.docker.com/pricing/). Complying is your organisation's "
		"responsibility. Running Windows 10/11 in a virtual machine needs a "
		"Windows licence that covers it."),
	"linux": (
		"NetRollout runs on Docker Engine (open source, Apache License 2.0)."),
}


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
	hostname: str
	https_port: int
	monitoring: bool
	org_certificate: bool
	timezone: str


# ── each answer's check ──

def check_hostname(value: str) -> str:
	value = value.strip().rstrip(".").lower()
	if not value:
		raise Invalid("A hostname is needed (the certificate is made for it).")
	try:
		return SETTINGS["public_hostname"].parse(value)
	except ValueError as e:
		raise Invalid(str(e)) from e


def check_port(value, busy: dict[int, str]) -> int:
	try:
		port = SETTINGS["https_port"].parse(str(value).strip())
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
	Standard Time) -> its IANA name (CLDR's mapping, via tzlocal)."""
	value = value.strip()
	for name in (value, win_tz.get(value)):
		if name and _is_zone(name):
			return name
	raise Invalid(f"Unknown timezone '{value}' - e.g. Europe/London, "
	              f"Asia/Jerusalem, UTC.")


def _is_zone(name: str) -> bool:
	try:
		zoneinfo.ZoneInfo(name)
		return True
	except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
		# OSError: a name that isn't a path on this OS (e.g. "?" on Windows)
		return False


def check_yes_no(value: str) -> bool:
	value = value.strip().lower()
	if value in ("y", "yes"):
		return True
	if value in ("n", "no"):
		return False
	raise Invalid("Answer y or n.")


def check_org_certificate(hostname: str) -> None:
	"""The organisation's certificate is in the certs folder and usable."""
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
	return next((p for p in (443, *ALTERNATIVE_PORTS) if p not in busy), 443)


def default_hostname(facts: Facts) -> str:
	try:
		return check_hostname(facts.computer_name)
	except Invalid:
		return FALLBACK_HOSTNAME


def default_timezone(facts: Facts) -> str:
	try:
		return check_timezone(facts.timezone) if facts.timezone else "UTC"
	except Invalid:
		return "UTC"


# ── asking ──

Read = Callable[[str], str]
Write = Callable[[str], None]


class NoAnswer(Exception):
	"""Input ended (not a terminal) while a question waited."""


def ask(question: str, default: str, check: Callable, read: Read,
        write: Write):
	"""`question [default]: ` until check() accepts the answer (Enter: the
	default)."""
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
	or — not interactive — their defaults. Raises Invalid naming every bad
	given answer at once."""
	busy = facts.busy_ports
	questions = [
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
	values, problems = {}, []
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


def accept_licence(os_name: str, given: bool, interactive: bool,
                   read: Read = input, write: Write = print) -> bool:
	"""The terms shown, then `yes` typed (or --yes)."""
	if given:
		return True
	if not interactive:
		return False
	write("")
	for part in ("common", os_name if os_name in LICENCE else "linux"):
		write(LICENCE[part])
		write("")
	try:
		return read("Type yes to accept and continue: ").strip().lower() == "yes"
	except EOFError:
		return False
