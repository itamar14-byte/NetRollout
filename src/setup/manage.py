"""What the scripts ask of the setup core after the install: the port-80
switch and the server's addresses before a start (prepare_start), and the
`netrollout status` report (status). The scripts gather what only the host
sees (busy ports, the containers' states, whether the address answers from
this computer) and pass it in; .env is only ever edited here."""
import datetime
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from src import certs, runtime, site_env
from src.setup import files

# The .env keys the scripts may change; never a secret
SCRIPT_KEYS = ("COMPOSE_FILE", "NETROLLOUT_SERVER_IPS", "HTTPS_PORT",
               "COMPOSE_PROFILES", "TZ")
CORE_SERVICES = ("app", "nginx", "postgres", "redis")
MONITORING_SERVICES = ("prometheus", "loki", "alloy", "grafana", "grafana-setup")
CERT_WARNING_DAYS = 30
HEALTH_URL = "http://app:8080/_netrollout/health"   # over the compose network


# ── .env ──

def env_read() -> dict[str, str]:
	values = {}
	for line in files.env_path().read_text(encoding="utf-8").splitlines():
		key, sep, value = line.partition("=")
		if sep and key and not key.startswith("#"):
			values[key.strip()] = value.strip()
	return values


def env_set(updates: dict[str, str]) -> bool:
	"""Change script-owned lines of .env in place (its comments, order and
	permissions kept — the file is rewritten, not replaced). True if it
	changed."""
	bad = [k for k in updates if k not in SCRIPT_KEYS]
	if bad:
		raise ValueError(f"not a script-owned .env key: {', '.join(bad)}")
	path = files.env_path()
	lines = path.read_text(encoding="utf-8").splitlines()
	pending, out = dict(updates), []
	for line in lines:
		key = line.partition("=")[0].strip()
		if key in pending and not line.lstrip().startswith("#"):
			out.append(f"{key}={pending.pop(key)}")
		else:
			out.append(line)
	out += [f"{k}={v}" for k, v in pending.items()]
	text = "\n".join(out) + "\n"
	if text == path.read_text(encoding="utf-8"):
		return False
	with open(path, "w", encoding="utf-8", newline="\n") as f:
		f.write(text)
	return True


def _compose_files(env: dict[str, str]) -> list[str]:
	return [f for f in env.get("COMPOSE_FILE", files.COMPOSE).split(",") if f]


def port80_owner(busy: dict[int, str]) -> str:
	who = busy.get(80, "")
	return f" (by {who})" if who else ""


# ── before a start ──

def prepare_start(busy: dict[int, str], server_ips: list[str]) -> list[str]:
	"""The port-80 switch follows whether port 80 is free (`busy` must not
	list NetRollout's own published ports); the server's addresses are
	refreshed. Returns what to tell the admin."""
	env = env_read()
	compose = _compose_files(env)
	said, updates = [], {}
	has_http = files.COMPOSE_HTTP in compose
	if 80 in busy and has_http:
		updates["COMPOSE_FILE"] = ",".join(f for f in compose if f != files.COMPOSE_HTTP)
		said.append(f"Port 80 is in use{port80_owner(busy)} - starting without "
		            f"the http -> https redirect (it comes back by itself once "
		            f"port 80 is free).")
	elif 80 not in busy and not has_http:
		updates["COMPOSE_FILE"] = ",".join(compose + [files.COMPOSE_HTTP])
		said.append("Port 80 is free - the http -> https redirect is on.")
	ips = ",".join(server_ips)
	if server_ips and env.get("NETROLLOUT_SERVER_IPS", "") != ips:
		updates["NETROLLOUT_SERVER_IPS"] = ips
	if updates:
		env_set(updates)
	return said


# ── netrollout status ──

@dataclass
class Observed:
	"""What the script saw on the host."""
	containers: dict[str, str] = field(default_factory=dict)  # service → state[/health]
	reachable: bool | None = None       # the address answered from this computer
	busy: dict[int, str] = field(default_factory=dict)


def parse_containers(text: str) -> dict[str, str]:
	""""app=running/healthy,nginx=running,…" """
	out = {}
	for item in filter(None, (x.strip() for x in text.split(","))):
		name, _, state = item.partition("=")
		out[name.strip()] = state.strip().lower()
	return out


def fetch_health(url: str = HEALTH_URL, timeout: float = 4.0) -> dict | None:
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


def status(seen: Observed, health: dict | None,
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

	nginx = _read_json(site_env.folder() / "status.json")
	if nginx:
		state = nginx.get("state", "?")
		when = nginx.get("time", "")
		if state == "rejected":
			lines.append(f"nginx:        REJECTED the last change ({when}): "
			             f"{nginx.get('message', '')} - it keeps serving the last good site")
			todo.append("Fix what nginx rejected: System Settings / Server Management "
			            "show the same message")
		else:
			lines.append(f"nginx:        {state} ({when})")

	lines.append("Certificate:  " + _certificate(now, todo))

	compose = _compose_files(env)
	if files.COMPOSE_HTTP in compose:
		lines.append("Port 80:      redirects to HTTPS")
	else:
		lines.append(f"Port 80:      off{' - in use' + port80_owner(seen.busy) if 80 in seen.busy else ''}")

	request = site_env.read().get(site_env.PORT_REQUEST)
	if request and request != port:
		lines.append(f"Port change:  {request} requested, {port} in use - run "
		             f"netrollout apply")

	if todo:
		lines.append("")
		lines.append("What to do:")
		lines += [f"  - {t}" for t in todo]
	return lines, not todo


def _certificate(now, todo) -> str:
	folder = runtime.certs_dir()
	try:
		cert = (folder / certs.CERT_FILE).read_bytes()
		key = (folder / certs.KEY_FILE).read_bytes()
	except OSError:
		todo.append("No certificate: upload one in Server Management, or generate a "
		            "self-signed one there")
		return "MISSING"
	check = certs.validate(cert, key, site_env.read().get(site_env.HOSTNAME) or None)
	kind = "self-signed" if certs.is_selfsigned(folder) else "your organisation's"
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


def _read_json(path) -> dict | None:
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None
	return data if isinstance(data, dict) else None
