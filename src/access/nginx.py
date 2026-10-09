"""What nginx serves, from the app's side.

The app writes two values — the hostname (System Settings) and the HTTPS port
that is actually published — into site.env in the folder it shares with nginx
(config/nginx). The nginx image's watcher (deploy/nginx/) validates them,
renders its own template, tests the result with `nginx -t` and reloads — or
keeps serving the last good site — and reports in status.json. The app never
writes nginx syntax.
"""
import datetime
import ipaddress
import json
import os
import re
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError

from src import runtime
from src.access import certs, site_env, port
from src.db.settings import SETTINGS, SettingsStore


SITE_FILE = site_env.FILE
STATUS_FILE = "status.json"
HOSTNAME_SEED_ENV = cast(str, SETTINGS["public_hostname"].env)   # it has one
SERVER_IPS_ENV = "NETROLLOUT_SERVER_IPS"
# A self-signed certificate reissued for a new hostname keeps the previous
# names this long, so people still typing one reach the redirect without a
# name warning — then they're dropped (the deadlines: OLD_NAMES_FILE).
OLD_NAMES_FILE = ".old-names.json"     # in the certs folder
NAME_TRANSITION_DAYS = 7
UPKEEP_INTERVAL_SECONDS = 3600
# a hostname save and the upkeep thread never reissue at the same moment
_cert_lock = threading.Lock()

Undo = Callable[[], None]   # puts the previous files back


class VerdictState(StrEnum):
	"""nginx's verdict on a change: what its watcher writes in status.json
	(applied / rejected), or the app's reading when there is none."""
	APPLIED = "applied"
	REJECTED = "rejected"          # the last good site keeps serving
	NOT_MANAGED = "not_managed"    # no NetRollout nginx reports here (verdict())
	NO_ANSWER = "no_answer"        # none within the wait (verdict())
	UNKNOWN = "unknown"            # status.json can't be read


def write_site(hostname: str | None) -> bool:
	"""Hand nginx `hostname` (empty: no canonical name) and the port in use,
	in site.env. An unchanged file isn't rewritten, so nginx isn't reloaded
	for nothing.

	:returns: whether it changed
	:raises ValueError: an invalid hostname
	:raises OSError: the folder can't be written"""
	return site_env.update(_site_values(hostname))


def _site_values(hostname: str | None) -> dict[str, str]:
	""":returns: what write_site writes into site.env
	:raises ValueError: an invalid hostname"""
	host = cast(str, SETTINGS["public_hostname"].parse(hostname or ""))
	return {site_env.HOSTNAME: host, site_env.HTTPS_PORT: str(port.serving_port())}


def seed_hostname_from_site() -> None:
	"""Before the first start's settings seed: the installer writes the
	hostname into site.env (not .env), so it becomes the System Settings
	hostname — unless the environment names one (non-Docker runs). Only a
	missing setting is ever seeded, so later starts change nothing."""
	try:
		host = site_env.read().get(site_env.HOSTNAME, "")
	except OSError:
		return
	if host and not os.environ.get(HOSTNAME_SEED_ENV):
		os.environ[HOSTNAME_SEED_ENV] = host


def server_ips() -> list[str]:
	"""The server's IP addresses, recorded by the installer
	(NETROLLOUT_SERVER_IPS: comma or space separated); invalid ones skipped."""
	out: list[str] = []
	for value in re.split(r"[,\s]+", os.environ.get(SERVER_IPS_ENV, "")):
		try:
			ip = str(ipaddress.ip_address(value.strip()))
		except ValueError:
			continue
		if ip not in out:
			out.append(ip)
	return out


class ProxyError(Exception):
	"""A hostname change couldn't be prepared; the message is for the page.
	Nothing was left changed."""


def _snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
	""":returns: each file's content (None: it doesn't exist), for _restore"""
	return {p: (p.read_bytes() if p.is_file() else None) for p in paths}


def _restore(saved: dict[Path, bytes | None]) -> None:
	"""Write each file's content atomically (the key owner-only); None
	deletes it - a _snapshot put back, or new files written."""
	for path, data in saved.items():
		if data is None:
			path.unlink(missing_ok=True)
			continue
		runtime.write_atomic(path, data, 0o600 if path.name == certs.KEY_FILE else 0o644)


def _put_back(saved: dict[Path, bytes | None],
              written: dict[Path, bytes | None], what: str) -> None:
	"""An undo's certificate files, under _cert_lock: `saved` is put back only
	while the files are still the ones the change wrote (`written`) - a
	change made since (another upload, a reissue) is left in place, and the
	log says so."""
	if _snapshot(list(written)) != written:
		print(f"[NetRollout] {what}: the certificate files weren't put back - "
		      f"they were changed again since; the newer ones stay", flush=True)
		return
	_restore(saved)


def _cert_undo(saved: dict[Path, bytes | None], what: str) -> Undo:
	"""undo() for a certificate change that has just written its files (call
	it under _cert_lock, right after): see _put_back."""
	written = _snapshot(list(saved))

	def undo() -> None:
		with _cert_lock:
			_put_back(saved, written, what)
	return undo


def change_hostname(new: str) -> Undo:
	"""Prepare nginx for the hostname `new`: the certificate first — a
	self-signed one is reissued for the new name (keeping the addresses it
	covered, and its previous names for NAME_TRANSITION_DAYS); an
	organisation's must already cover it — then site.env.

	:returns: undo(), which puts the previous certificate files and hostname
	 back - only the hostname key of site.env (a port request or the port
	 helper's keys written meanwhile stay), and only what no other change has
	 replaced since
	:raises ProxyError: the reason, having changed nothing"""
	with _cert_lock:
		return _change_hostname(new)


def _change_hostname(new: str) -> Undo:
	"""change_hostname's work, under the certificate lock."""
	cert_dir = runtime.certs_dir()
	cert = cert_dir / certs.CERT_FILE
	saved = _snapshot(_cert_files(cert_dir))
	try:
		previous, existed = site_env.read(), site_env.path().is_file()
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
		values = _site_values(new)    # what write_site writes, for the undo
		write_site(new)     # the last step: when it raises, site.env is as it was
	except ProxyError:
		_restore(saved)
		raise
	except OSError as e:
		_restore(saved)
		raise ProxyError(f"NetRollout couldn't write {e.filename or site_env.folder()}: "
		                 f"{e.strerror or e}. Nothing was changed.") from e
	except ValueError as e:
		_restore(saved)
		raise ProxyError(f"{e}. Nothing was changed.") from e
	written = _snapshot(list(saved))

	def undo() -> None:
		with _cert_lock:
			_put_back(saved, written, f"undoing the hostname {new}")
			# only the keys this change wrote, while the hostname is still its
			left = site_env.put_back(previous, values, existed, guard=site_env.HOSTNAME)
			if left:
				print(f"[NetRollout] undoing the hostname {new}: site.env's "
				      f"{', '.join(left)} changed again since; left as it is",
				      flush=True)
	return undo


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
	"""Keep the previous names' deadlines ({name: until}); none: the file goes."""
	path = cert_dir / OLD_NAMES_FILE
	if not names:
		path.unlink(missing_ok=True)
		return
	_restore({path: json.dumps(names, indent=1, sort_keys=True).encode()})


def drop_expired_names(now: float | None = None) -> list[str]:
	"""Reissue NetRollout's self-signed certificate without the previous
	hostnames whose transition period ended (nginx reloads it, no restart).
	An organisation's certificate is never touched.

	:param now: the time (epoch seconds; tests); now when None
	:returns: the names dropped"""
	with _cert_lock:
		cert_dir = runtime.certs_dir()
		cert = cert_dir / certs.CERT_FILE
		stored = _read_old_names(cert_dir)
		if not stored or not cert.is_file() or not certs.is_selfsigned(cert_dir):
			return []
		now = time.time() if now is None else now
		keep = {n: u for n, u in stored.items() if u > now}
		pem = cert.read_bytes()
		dns, ips = certs.names_in(pem)
		# the hostname is the common name (an IP address isn't among the DNS names)
		host = certs.common_name(pem)
		others = [n for n in dns if n != host]
		expired = [n for n in others if n in stored and n not in keep]
		if expired:
			certs.selfsigned(host, [str(ip) for ip in ips], cert_dir,
			                 also_names=[n for n in others if n not in expired])
		_write_old_names(cert_dir, keep)
		return expired


def start_certificate_upkeep() -> None:
	"""drop_expired_names() now and every hour, from a daemon thread — a
	server that never restarts still drops them. Called by the web app's
	entry point. Never raises."""
	def loop() -> None:
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


def _cert_files(cert_dir: Path) -> list[Path]:
	"""Everything a certificate change replaces — the undo snapshot."""
	return [cert_dir / certs.CERT_FILE, cert_dir / certs.KEY_FILE,
	        cert_dir / certs.SELFSIGNED_MARKER, cert_dir / OLD_NAMES_FILE]


def install_certificate(cert_pem: bytes, key_pem: bytes,
                        hostname: str | None) -> tuple[certs.CertCheck, Undo]:
	"""Use an organisation's certificate + key: checked first (certs.validate,
	against the saved `hostname`), then written key first (nginx's watcher
	tests the pair before using it). The self-signed marker goes, so
	NetRollout never reissues it.

	:returns: (the check - its warnings for the page, undo)
	:raises ProxyError: every problem found, having changed nothing"""
	check = certs.validate(cert_pem, key_pem, hostname or None)
	if not check.ok:
		raise ProxyError(" ".join(check.problems))
	with _cert_lock:
		cert_dir = runtime.certs_dir()
		saved = _snapshot(_cert_files(cert_dir))
		try:
			cert_dir.mkdir(parents=True, exist_ok=True)
			_restore({cert_dir / certs.KEY_FILE: key_pem})
			_restore({cert_dir / certs.CERT_FILE: cert_pem})
			(cert_dir / certs.SELFSIGNED_MARKER).unlink(missing_ok=True)
			(cert_dir / OLD_NAMES_FILE).unlink(missing_ok=True)
		except OSError as e:
			_restore(saved)
			raise ProxyError(f"NetRollout couldn't write {e.filename or cert_dir}: "
			                 f"{e.strerror or e}. Nothing was changed.") from e
		return check, _cert_undo(saved, "undoing the certificate upload")


def generate_selfsigned(hostname: str | None) -> Undo:
	"""Replace the certificate with a new self-signed one for `hostname`
	(else the name the current certificate is for), for the server's IP
	addresses (server_ips()) and any the current one covers.

	:returns: undo()
	:raises ProxyError: no name to make it for, or it couldn't be written -
	 having changed nothing"""
	with _cert_lock:
		cert_dir = runtime.certs_dir()
		cert = cert_dir / certs.CERT_FILE
		dns: list[str] = []
		ips: list[certs.SanIP] = []
		try:
			if cert.is_file():
				dns, ips = certs.names_in(cert.read_bytes())
		except (OSError, ValueError):
			pass                          # unreadable: start from the hostname
		name = hostname or (dns[0] if dns else "")
		if not name:
			raise ProxyError("Set the hostname first (System Settings → Access): "
			                 "the certificate is made for it.")
		saved = _snapshot(_cert_files(cert_dir))
		try:
			addresses = server_ips()
			addresses += [str(ip) for ip in ips if str(ip) not in addresses]
			certs.selfsigned(name, addresses, cert_dir)
			(cert_dir / OLD_NAMES_FILE).unlink(missing_ok=True)
		except (OSError, ValueError) as e:
			_restore(saved)
			where = getattr(e, "filename", None) or cert_dir
			raise ProxyError(f"NetRollout couldn't write {where}: "
			                 f"{getattr(e, 'strerror', None) or e}. Nothing was "
			                 f"changed.") from e
		return _cert_undo(saved, "undoing the self-signed certificate")


def verdict(managed: bool, started: float, hostname: str | None = None) -> dict[str, Any]:
	"""nginx's answer to a change written at `started`: applied / rejected,
	or why there is none — not_managed (no NetRollout nginx reports here) or
	no_answer. With `hostname`: the answer for that hostname.

	:param managed: whether a NetRollout nginx reports here
	:param started: when the change was written (epoch seconds)
	:returns: {"state", "message"?}"""
	if not managed:
		return {"state": VerdictState.NOT_MANAGED}
	status = wait_for_status(started, hostname=hostname)
	if status is None:
		return {"state": VerdictState.NO_ANSWER}
	return {"state": status.get("state"), "message": status.get("message")}


def overview(hostname: str | None) -> dict[str, Any]:
	"""What the Access card shows, whenever an admin looks — not only right
	after a save: nginx's last verdict and the certificate in use, checked
	against `hostname` (the saved one).
	{"nginx": None (no NetRollout nginx reports here) | {"state", "message",
	 "time"}, "certificate": None (no file) | {"names", "not_after",
	 "selfsigned", "problems", "warnings", "old_names": [{"name", "until"}]}}"""
	status = read_status()
	nginx = None if status is None else {
		"state": status.get("state", VerdictState.UNKNOWN),
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


def read_status() -> dict[str, Any] | None:
	"""The watcher's last verdict: {"state": "applied" | "rejected",
	"message", "time"}. None when no nginx reports here (an external proxy,
	or dev without the stack) — "not managed"."""
	try:
		data = json.loads((site_env.folder() / STATUS_FILE).read_text(encoding="utf-8"))
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		return {"state": VerdictState.UNKNOWN, "message": "nginx's status.json can't be read",
		        "time": None}
	if not isinstance(data, dict):
		return {"state": VerdictState.UNKNOWN, "message": "nginx's status.json can't be read",
		        "time": None}
	return data


def _status_time(status: dict[str, Any]) -> float | None:
	""":returns: when the watcher wrote it (epoch seconds); None: unreadable"""
	try:
		return datetime.datetime.strptime(status["time"], "%Y-%m-%dT%H:%M:%SZ") \
			.replace(tzinfo=datetime.timezone.utc).timestamp()
	except (KeyError, TypeError, ValueError):
		return None


def wait_for_status(after: float, timeout: float = 8.0, poll: float = 0.5,
                    hostname: str | None = None) -> dict[str, Any] | None:
	"""The first verdict the watcher writes at or after `after` (epoch
	seconds; it checks every 3 s), or None if none comes within `timeout`.
	With `hostname`: only a rejection, or the site applied for that name —
	not an earlier reload (e.g. of a reissued certificate alone)."""
	deadline = time.monotonic() + timeout
	while True:
		status = read_status()
		written = _status_time(status) if status else None
		# the watcher's clock has whole seconds
		if status and written is not None and written >= int(after) and (
				hostname is None or status.get("state") == VerdictState.REJECTED
				or f"hostname={hostname or '(none)'} " in
				f"{status.get('message', '')} "):
			return status
		if time.monotonic() >= deadline:
			return None
		time.sleep(poll)


def sync_at_start(settings: SettingsStore) -> None:
	"""At every start nginx gets the saved hostname — also after a change made
	while it was down, or a restore. Never stops the app from starting."""
	try:
		changed = write_site(settings.get("public_hostname"))
	except (ValueError, OSError, SQLAlchemyError) as e:
		print(f"[NetRollout] nginx site values not written ({site_env.folder()}): "
		      f"{e}", flush=True)
		return
	if changed:
		print(f"[NetRollout] nginx site values written ({site_env.folder() / SITE_FILE})",
		      flush=True)
