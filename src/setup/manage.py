"""What the scripts ask of the setup core after the install: the port-80
switch and the server's addresses before a start (prepare_start), the
restored backup's encryption key into .env (restore_key), and the
`netrollout status` report (status). The scripts gather what only the host
sees (busy ports, the containers' states, whether the address answers from
this computer) and pass it in; .env is only ever edited through env.py.
An update's direction and .env: update.py."""
import datetime
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from cryptography.fernet import Fernet

from src import runtime
from src.access import certs, site_env
from src.access.nginx import VerdictState, read_status
from src.backup import archive
from src.setup.env import env_write, env_read, env_set, COMPOSE_HTTP, compose_files


CORE_SERVICES = ("app", "nginx", "postgres", "redis")
MONITORING_SERVICES = ("prometheus", "loki", "alloy", "grafana", "grafana-setup")
CERT_WARNING_DAYS = 30
HEALTH_URL = "http://app:8080/_netrollout/health"   # over the compose network
# A restore (python -m src.backup restore --key-out) leaves the backup's key
# here for restore_key; the backups folder is closed to everyone else
RESTORED_KEY = ".restored-key"


def port80_owner(busy: dict[int, str]) -> str:
	who = busy.get(80, "")
	return f" (by {who})" if who else ""


# ── before a start ──

def prepare_start(busy: dict[int, str], server_ips: list[str]) -> list[str]:
	"""The port-80 switch follows whether port 80 is free (`busy` must not
	list NetRollout's own published ports); the server's addresses are
	refreshed. Returns what to tell the admin."""
	env = env_read()
	compose = compose_files(env)
	said, updates = [], {}
	has_http = COMPOSE_HTTP in compose
	if 80 in busy and has_http:
		updates["COMPOSE_FILE"] = ",".join(f for f in compose if f != COMPOSE_HTTP)
		said.append(f"Port 80 is in use{port80_owner(busy)} - starting without "
		            f"the http -> https redirect (it comes back by itself once "
		            f"port 80 is free).")
	elif 80 not in busy and not has_http:
		updates["COMPOSE_FILE"] = ",".join(compose + [COMPOSE_HTTP])
		said.append("Port 80 is free - the http -> https redirect is on.")
	ips = ",".join(server_ips)
	if server_ips and env.get("NETROLLOUT_SERVER_IPS", "") != ips:
		updates["NETROLLOUT_SERVER_IPS"] = ips
	if updates:
		env_set(updates)
	return said


# ── after a restore ──

def restore_key() -> str:
	"""The restored backup's encryption key into .env — the saved credentials
	were encrypted with it — and the hand-over file removed. Returns what to
	say. :raises ValueError: no key handed over, or not a key"""
	path = runtime.backups_dir() / RESTORED_KEY
	try:
		key = path.read_text(encoding="utf-8").strip()
	except FileNotFoundError:
		raise ValueError(f"No restored key in {path} - run the restore first.") from None
	except OSError as e:
		raise ValueError(f"Couldn't read {path}: {e.strerror or e}.") from None
	try:
		Fernet(key)
	except ValueError:
		raise ValueError(f"{path} doesn't hold an encryption key.") from None
	changed = env_write({"NETROLLOUT_ENCRYPTION_KEY": key})
	path.unlink()
	return ("The backup's encryption key is in .env now (it was made on another "
	        "installation)." if changed else "The encryption key is unchanged.")


# ── netrollout status ──

@dataclass
class Observed:
	"""What the script saw on the host."""
	containers: dict[str, str] = field(default_factory=dict)  # service → state[/health]
	reachable: bool | None = None       # the address answered from this computer
	busy: dict[int, str] = field(default_factory=dict)


def parse_containers(text: str) -> dict[str, str]:
	""""app=running/healthy,nginx=running,…" → {service: state}"""
	out: dict[str, str] = {}
	for item in filter(None, (x.strip() for x in text.split(","))):
		name, _, state = item.partition("=")
		out[name.strip()] = state.strip().lower()
	return out


def fetch_health(url: str = HEALTH_URL,
                 timeout: float = 4.0) -> dict[str, Any] | None:
	""":returns: the app's health report - also a 503's, which says what's
	 down; None when there's no answer"""
	try:
		with urllib.request.urlopen(url, timeout=timeout) as r:
			return json.loads(r.read().decode())
	except urllib.error.HTTPError as e:      # 503: an answer, saying what's down
		try:
			return json.loads(e.read().decode())
		except ValueError:
			return None
	except (OSError, ValueError):
		return None


def address(env: dict[str, str]) -> str:
	host = site_env.read().get(site_env.HOSTNAME) or "localhost"
	port = env.get("HTTPS_PORT", "443")
	return f"https://{host}" + ("" if port == "443" else f":{port}")


def status(seen: Observed, health: dict[str, Any] | None,
           now: datetime.datetime | None = None) -> tuple[list[str], bool]:
	"""The report, one line per subject, then what to do; and whether all
	is well."""
	now = now or datetime.datetime.now(datetime.timezone.utc)
	env = env_read()
	lines, todo = [], []
	url = address(env)
	ips = [ip for ip in env.get("NETROLLOUT_SERVER_IPS", "").split(",") if ip]
	port = env.get("HTTPS_PORT", "443")
	others = [f"https://{ip}" + ("" if port == "443" else f":{port}") for ip in ips]
	lines.append(f"NetRollout {runtime.VERSION}")
	lines.append(f"Address:      {url}" + (f"  (also {', '.join(others)})" if others else ""))

	monitoring = "monitoring" in env.get("COMPOSE_PROFILES", "")
	expected = CORE_SERVICES + (MONITORING_SERVICES if monitoring else ())
	states, down = [], []
	for name in expected:
		state = seen.containers.get(name, "not running")
		good = state.startswith("running") and "unhealthy" not in state
		states.append(f"{name} {'ok' if good else state.upper()}")
		if not good:
			down.append(name)
	lines.append("Containers:   " + ", ".join(states))
	if down:
		todo.append("Start it: netrollout start  (if it stays down: netrollout logs "
		            f"{down[0]})")

	if health is None:
		lines.append("Health:       the app isn't answering")
		if "app" not in down:
			todo.append("The app runs but doesn't answer: netrollout logs app")
	else:
		bad = [n for n in ("postgres", "redis") if not health.get(n)]
		lines.append("Health:       " + ("ok (database, Redis)" if not bad else
		             f"{' and '.join('the database' if b == 'postgres' else 'Redis' for b in bad)} unreachable"))
		r = health.get("rollouts") or {}
		running, queued = r.get("running", 0), r.get("queued", 0)
		lines.append("Rollouts:     " + (f"{running} running, {queued} queued"
		                                 if running or queued else "none running"))
		if health.get("draining"):
			lines.append("              stopping or restarting - new rollouts wait")
		if bad:
			todo.append("Check the database / Redis: netrollout logs postgres  /  netrollout logs redis")

	if seen.reachable is not None:
		lines.append(f"From here:    {url} " + ("answers" if seen.reachable
		                                         else "does NOT answer"))
		if not seen.reachable and not down:
			todo.append("Everything runs but the address doesn't answer from this "
			            "computer: check the name (DNS) and the firewall for port "
			            f"{port}")

	nginx = read_status()
	# unreadable ("unknown"): no line, as before
	if nginx and nginx.get("state") != VerdictState.UNKNOWN:
		state = nginx.get("state", "?")
		when = nginx.get("time", "")
		if state == VerdictState.REJECTED:
			lines.append(f"nginx:        REJECTED the last change ({when}): "
			             f"{nginx.get('message', '')} - it keeps serving the last good site")
			todo.append("Fix what nginx rejected: System Settings / Server Management "
			            "show the same message")
		else:
			lines.append(f"nginx:        {state} ({when})")

	lines.append("Certificate:  " + _certificate(now, todo))

	compose = compose_files(env)
	if COMPOSE_HTTP in compose:
		lines.append("Port 80:      redirects to HTTPS")
	else:
		lines.append(f"Port 80:      off{' - in use' + port80_owner(seen.busy) if 80 in seen.busy else ''}")

	lines.append("Backups:      " + _backups(todo))

	request = site_env.read().get(site_env.PORT_REQUEST)
	if request and request != port:
		lines.append(f"Port change:  {request} requested, {port} in use - run "
		             f"netrollout apply")

	if todo:
		lines.append("")
		lines.append("What to do:")
		lines += [f"  - {t}" for t in todo]
	return lines, not todo


def _backups(todo: list[str]) -> str:
	"""The status' Backups line: how many, their size, the newest - and a
	failed scheduled one.

	:param todo: what to do; a failed scheduled backup adds to it"""
	folder = archive.BackupFolder.app()
	entries = folder.entries()
	text = (f"{len(entries)}, {sum(e.size for e in entries) / 1048576:.1f} MB "
	        f"in backups" if entries else "none yet")
	if entries and entries[0].manifest:
		text += f", newest {entries[0].manifest.created.replace('T', ' ')[:16]}"
	last = folder.status()
	if last and not last.get("ok"):
		text += " - the last scheduled one FAILED"
		todo.append(f"The last scheduled backup failed ({last.get('message', '')}): "
		            f"see System Settings -> Backups")
	return text


def _certificate(now: datetime.datetime, todo: list[str]) -> str:
	"""The status' Certificate line: whose, valid until when, any problem.

	:param todo: what to do; missing, unreadable, a problem or expiring soon
	 add to it"""
	store = certs.CertificateStore()
	hostname = site_env.read().get(site_env.HOSTNAME) or None
	try:
		check = store.check(hostname)
	except OSError:
		todo.append("No certificate: upload one in Server Management, or generate a "
		            "self-signed one there")
		return "MISSING"
	kind = "self-signed" if store.selfsigned else "your organisation's"
	if not check.not_after:
		todo.append("The certificate can't be read: upload it again in Server Management")
		return f"{kind}, unreadable"
	days = (check.not_after - now).days
	text = f"{kind}, valid until {check.not_after:%Y-%m-%d} ({days} days)"
	if check.problems:
		todo.append("Certificate: " + " ".join(check.problems))
		return text + " - PROBLEM"
	if days < CERT_WARNING_DAYS:
		todo.append(f"The certificate expires in {days} days: upload a new one, or "
		            f"generate a self-signed one, in Server Management")
		return text + " - EXPIRES SOON"
	return text

