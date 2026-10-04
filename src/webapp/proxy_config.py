"""What nginx serves, from the app's side.

The app writes two values — the hostname (System Settings) and the HTTPS port
that is actually published — into site.env in the folder it shares with nginx
(config/nginx). The nginx image's watcher (deploy/nginx/) validates them,
renders its own template, tests the result with `nginx -t` and reloads — or
keeps serving the last good site — and reports in status.json. The app never
writes nginx syntax.
"""
import datetime
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from src import certs, runtime
from src.db.settings import SETTINGS
from src.webapp import port_apply

SITE_FILE = "site.env"
STATUS_FILE = "status.json"
APPLIED_PORT_ENV = port_apply.PUBLISHED_PORT_ENV
# A self-signed certificate reissued for a new hostname keeps the previous
# names this long, so people still typing one reach the redirect without a
# name warning — then they're dropped (the deadlines: OLD_NAMES_FILE).
OLD_NAMES_FILE = ".old-names.json"     # in the certs folder
NAME_TRANSITION_DAYS = 7
UPKEEP_INTERVAL_SECONDS = 3600
# a hostname save and the upkeep thread never reissue at the same moment
_cert_lock = threading.Lock()


def shared_dir() -> Path:
	return runtime.config_dir() / "nginx"


def applied_https_port() -> int:
	"""The port NetRollout is served on — not the System Settings value, which
	is the port wanted: redirects keep using this one until the port helper
	applies the new one (port_apply.serving_port)."""
	return port_apply.serving_port()


def write_site(hostname: str | None) -> bool:
	"""Write site.env for `hostname` (empty: no canonical name). True if it
	changed — an unchanged file isn't rewritten, so nginx isn't reloaded for
	nothing. Raises ValueError for an invalid hostname, OSError when the
	folder can't be written."""
	host = SETTINGS["public_hostname"].parse(hostname or "")
	content = (f"NETROLLOUT_HOSTNAME={host}\n"
	           f"NETROLLOUT_HTTPS_PORT={applied_https_port()}\n")
	folder = shared_dir()
	folder.mkdir(parents=True, exist_ok=True)
	target = folder / SITE_FILE
	if target.is_file() and target.read_text(encoding="utf-8") == content:
		return False
	# a temp file + rename: the watcher sees the old file or the new one
	fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{SITE_FILE}.")
	try:
		with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
			f.write(content)
		os.chmod(tmp, 0o644)
		os.replace(tmp, target)
	except BaseException:
		if os.path.exists(tmp):
			os.remove(tmp)
		raise
	return True


class ProxyError(Exception):
	"""A hostname change couldn't be prepared; the message is for the page.
	Nothing was left changed."""


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
	return {p: (p.read_bytes() if p.is_file() else None) for p in paths}


def _restore(saved: dict[Path, bytes | None]) -> None:
	for path, data in saved.items():
		if data is None:
			path.unlink(missing_ok=True)
			continue
		fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
		with os.fdopen(fd, "wb") as f:
			f.write(data)
		os.chmod(tmp, 0o600 if path.name == certs.KEY_FILE else 0o644)
		os.replace(tmp, path)


def change_hostname(new: str):
	"""Prepare nginx for the hostname `new`: the certificate first — a
	self-signed one is reissued for the new name (keeping the addresses it
	covered); an organisation's must already cover it — then site.env.
	Returns undo(), which puts the previous files back. Raises ProxyError
	with the reason, having changed nothing."""
	with _cert_lock:
		return _change_hostname(new)


def _change_hostname(new: str):
	cert_dir = runtime.certs_dir()
	cert = cert_dir / certs.CERT_FILE
	saved = _snapshot([shared_dir() / SITE_FILE, cert, cert_dir / certs.KEY_FILE,
	                   cert_dir / certs.SELFSIGNED_MARKER,
	                   cert_dir / OLD_NAMES_FILE])
	try:
		if new and cert.is_file():
			dns, ips = certs.names_in(cert.read_bytes())
			if certs.is_selfsigned(cert_dir):
				# the names it covered stay for a transition period
				now = time.time()
				old = {n: u for n, u in _read_old_names(cert_dir).items() if u > now}
				for name in dns:
					old.setdefault(name, now + NAME_TRANSITION_DAYS * 86400)
				old.pop(new, None)
				certs.selfsigned(new, [str(ip) for ip in ips], cert_dir,
				                 also_names=sorted(old))
				_write_old_names(cert_dir, old)
			elif not certs.host_matches(new, dns, ips):
				covers = ", ".join([*dns, *map(str, ips)]) or "no names"
				raise ProxyError(
					f"The certificate in use covers {covers} — not {new}. Upload "
					f"a certificate for {new} first (Server Management → "
					f"Certificate), then change the hostname.")
		write_site(new)
	except ProxyError:
		_restore(saved)
		raise
	except OSError as e:
		_restore(saved)
		raise ProxyError(f"NetRollout couldn't write {e.filename or shared_dir()}: "
		                 f"{e.strerror or e}. Nothing was changed.") from e
	except ValueError as e:
		_restore(saved)
		raise ProxyError(f"{e}. Nothing was changed.") from e
	return lambda: _restore(saved)


def _read_old_names(cert_dir: Path) -> dict[str, float]:
	"""{name: until (epoch seconds)}; nothing readable → {}."""
	try:
		data = json.loads((cert_dir / OLD_NAMES_FILE).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	if not isinstance(data, dict):
		return {}
	return {n: float(u) for n, u in data.items()
	        if isinstance(n, str) and isinstance(u, (int, float))}


def _write_old_names(cert_dir: Path, names: dict[str, float]) -> None:
	path = cert_dir / OLD_NAMES_FILE
	if not names:
		path.unlink(missing_ok=True)
		return
	_restore({path: json.dumps(names, indent=1, sort_keys=True).encode()})


def drop_expired_names(now: float | None = None) -> list[str]:
	"""Reissue NetRollout's self-signed certificate without the previous
	hostnames whose transition period ended (nginx reloads it, no restart).
	Returns the names dropped. An organisation's certificate is never
	touched."""
	with _cert_lock:
		cert_dir = runtime.certs_dir()
		cert = cert_dir / certs.CERT_FILE
		stored = _read_old_names(cert_dir)
		if not stored or not cert.is_file() or not certs.is_selfsigned(cert_dir):
			return []
		now = time.time() if now is None else now
		keep = {n: u for n, u in stored.items() if u > now}
		dns, ips = certs.names_in(cert.read_bytes())
		expired = [n for n in dns[1:] if n in stored and n not in keep]
		if expired:
			# the first name is the hostname itself
			certs.selfsigned(dns[0], [str(ip) for ip in ips], cert_dir,
			                 also_names=[n for n in dns[1:] if n not in expired])
		_write_old_names(cert_dir, keep)
		return expired


def start_certificate_upkeep() -> None:
	"""drop_expired_names() now and every hour, from a daemon thread — a
	server that never restarts still drops them. Called by the web app's
	entry point. Never raises."""
	def loop():
		while True:
			try:
				dropped = drop_expired_names()
				if dropped:
					print(f"[NetRollout] certificate reissued without the previous "
					      f"hostname(s) {', '.join(dropped)} (transition of "
					      f"{NAME_TRANSITION_DAYS} days over)", flush=True)
			except Exception as e:      # noqa: BLE001 — keep the thread alive
				print(f"[NetRollout] certificate upkeep failed: {e}", flush=True)
			time.sleep(UPKEEP_INTERVAL_SECONDS)
	threading.Thread(target=loop, name="certificate-upkeep", daemon=True).start()


def overview(hostname: str | None) -> dict:
	"""What the Access card shows, whenever an admin looks — not only right
	after a save: nginx's last verdict and the certificate in use, checked
	against `hostname` (the saved one).
	{"nginx": None (no NetRollout nginx reports here) | {"state", "message",
	 "time"}, "certificate": None (no file) | {"names", "not_after",
	 "selfsigned", "problems", "warnings", "old_names": [{"name", "until"}]}}"""
	status = read_status()
	nginx = None if status is None else {
		"state": status.get("state", "unknown"),
		"message": status.get("message", ""), "time": status.get("time")}
	cert_dir = runtime.certs_dir()
	try:
		cert_pem = (cert_dir / certs.CERT_FILE).read_bytes()
	except FileNotFoundError:
		return {"nginx": nginx, "certificate": None}
	except OSError as e:
		return {"nginx": nginx, "certificate": {
			"names": [], "not_after": None, "selfsigned": False, "old_names": [],
			"problems": [f"NetRollout can't read the certificate: {e.strerror or e}."],
			"warnings": []}}
	try:
		key_pem = (cert_dir / certs.KEY_FILE).read_bytes()
	except OSError:
		key_pem = b""                    # validate() reports the key as missing
	check = certs.validate(cert_pem, key_pem, hostname or None)
	old = _read_old_names(cert_dir)
	return {"nginx": nginx, "certificate": {
		"names": check.names,
		"not_after": check.not_after.isoformat() if check.not_after else None,
		"selfsigned": certs.is_selfsigned(cert_dir),
		"problems": check.problems, "warnings": check.warnings,
		"old_names": [{"name": n, "until": old[n]} for n in check.names if n in old]}}


def read_status() -> dict | None:
	"""The watcher's last verdict: {"state": "applied" | "rejected",
	"message", "time"}. None when no nginx reports here (an external proxy,
	or dev without the stack) — "not managed"."""
	try:
		data = json.loads((shared_dir() / STATUS_FILE).read_text(encoding="utf-8"))
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		return {"state": "unknown", "message": "nginx's status.json can't be read",
		        "time": None}
	if not isinstance(data, dict):
		return {"state": "unknown", "message": "nginx's status.json can't be read",
		        "time": None}
	return data


def _status_time(status: dict) -> float | None:
	try:
		return datetime.datetime.strptime(status["time"], "%Y-%m-%dT%H:%M:%SZ") \
			.replace(tzinfo=datetime.timezone.utc).timestamp()
	except (KeyError, TypeError, ValueError):
		return None


def wait_for_status(after: float, timeout: float = 8.0, poll: float = 0.5,
                    hostname: str | None = None) -> dict | None:
	"""The first verdict the watcher writes at or after `after` (epoch
	seconds; it checks every 3 s), or None if none comes within `timeout`.
	With `hostname`: only a rejection, or the site applied for that name —
	not an earlier reload (e.g. of a reissued certificate alone)."""
	deadline = time.monotonic() + timeout
	while True:
		status = read_status()
		written = _status_time(status) if status else None
		# the watcher's clock has whole seconds
		if written is not None and written >= int(after) and (
				hostname is None or status.get("state") == "rejected"
				or f"hostname={hostname or '(none)'} " in
				f"{status.get('message', '')} "):
			return status
		if time.monotonic() >= deadline:
			return None
		time.sleep(poll)


def sync_at_start(settings) -> None:
	"""At every start nginx gets the saved hostname — also after a change made
	while it was down, or a restore. Never stops the app from starting."""
	try:
		changed = write_site(settings.get("public_hostname"))
	except (ValueError, OSError, SQLAlchemyError) as e:
		print(f"[NetRollout] nginx site values not written ({shared_dir()}): "
		      f"{e}", flush=True)
		return
	if changed:
		print(f"[NetRollout] nginx site values written ({shared_dir() / SITE_FILE})",
		      flush=True)
