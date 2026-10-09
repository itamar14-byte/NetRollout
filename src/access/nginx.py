"""What nginx serves, from the app's side.

The app writes two values — the hostname (System Settings) and the HTTPS port
that is actually published — into site.env in the folder it shares with nginx
(config/nginx). The nginx image's watcher (deploy/nginx/) validates them,
renders its own template, tests the result with `nginx -t` and reloads — or
keeps serving the last good site — and reports in status.json. The app never
writes nginx syntax.

Nginx is that contract seen from the app: whether a NetRollout nginx reports
here, its verdicts, and a change it must accept (a new hostname, a new
certificate - the certs folder is certs.CertificateStore) applied or undone.
"""
import datetime
import ipaddress
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from sqlalchemy.exc import SQLAlchemyError

from src.access import site_env, port
from src.access.certs import CertificateStore, ProxyError, Undo
from src.db.settings import SETTINGS, SettingsStore


SITE_FILE = site_env.FILE
STATUS_FILE = "status.json"
HOSTNAME_SEED_ENV = cast(str, SETTINGS["public_hostname"].env)   # it has one
SERVER_IPS_ENV = "NETROLLOUT_SERVER_IPS"


class VerdictState(StrEnum):
	"""nginx's verdict on a change: what its watcher writes in status.json
	(applied / rejected), or the app's reading when there is none."""
	APPLIED = "applied"
	REJECTED = "rejected"          # the last good site keeps serving
	NOT_MANAGED = "not_managed"    # no NetRollout nginx reports here (Nginx.apply)
	NO_ANSWER = "no_answer"        # none within the wait (Nginx.apply)
	UNKNOWN = "unknown"            # status.json can't be read


@dataclass(frozen=True)
class Verdict:
	"""nginx's verdict: its watcher's (status.json) - or why there is none
	(not_managed, no_answer)."""
	state: str | None              # a VerdictState, or what status.json says
	message: str | None = None
	time: str | None = None        # when the watcher wrote it (UTC, ISO 8601)

	@property
	def rejected(self) -> bool:
		""":returns: whether nginx refused the change (the last good site serves)"""
		return self.state == VerdictState.REJECTED

	def as_dict(self, with_time: bool = False) -> dict[str, Any]:
		"""The JSON the pages read: {"state"} when nginx didn't answer, else
		{"state", "message"}.

		:param with_time: add "time" (the Access card's last verdict)"""
		if self.state in (VerdictState.NOT_MANAGED, VerdictState.NO_ANSWER):
			return {"state": self.state}
		out: dict[str, Any] = {"state": self.state, "message": self.message}
		if with_time:
			out["time"] = self.time
		return out


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


class Nginx:
	"""NetRollout's nginx, from the app's side: whether one reports here, its
	last verdict, and the changes it must accept - each applied, or undone
	when nginx rejects it."""

	def __init__(self, certificates: CertificateStore) -> None:
		""":param certificates: the certs folder nginx serves from"""
		self.certificates = certificates

	@property
	def managed(self) -> bool:
		""":returns: whether a NetRollout nginx reports here (its status.json)"""
		return read_status() is not None

	def status(self) -> Verdict | None:
		""":returns: the watcher's last verdict (unreadable: "unknown"); None
		 when no nginx reports here"""
		status = read_status()
		if status is None:
			return None
		return Verdict(status.get("state", VerdictState.UNKNOWN),
		               status.get("message", ""), status.get("time"))

	def overview(self, hostname: str | None) -> dict[str, Any]:
		"""What the Access card shows, whenever an admin looks — not only right
		after a save: nginx's last verdict and the certificate in use, checked
		against `hostname` (the saved one).
		{"nginx": None (no NetRollout nginx reports here) | {"state", "message",
		 "time"}, "certificate": CertificateStore.summary}"""
		status = self.status()
		return {"nginx": None if status is None else status.as_dict(with_time=True),
		        "certificate": self.certificates.summary(hostname)}

	def change_hostname(self, new: str) -> Undo:
		"""Prepare nginx for the hostname `new`: the certificate first
		(CertificateStore.rename: a self-signed one is reissued, an
		organisation's must cover it), then site.env - both under the
		certificate lock.

		:returns: undo(), which puts the previous certificate files and hostname
		 back - only the hostname key of site.env (a port request or the port
		 helper's keys written meanwhile stay), and only what no other change has
		 replaced since
		:raises ProxyError: the reason, having changed nothing"""
		with self.certificates.lock:
			try:
				previous, existed = site_env.read(), site_env.path().is_file()
				undo_certificate = self.certificates.rename(new)
				try:
					values = _site_values(new)    # what write_site writes, for the undo
					write_site(new)     # the last step: when it raises, site.env is as it was
				except (OSError, ValueError):
					undo_certificate()
					raise
			except OSError as e:
				raise ProxyError(f"NetRollout couldn't write {e.filename or site_env.folder()}: "
				                 f"{e.strerror or e}. Nothing was changed.") from e
			except ValueError as e:
				raise ProxyError(f"{e}. Nothing was changed.") from e

		def undo() -> None:
			with self.certificates.lock:
				undo_certificate()
				# only the keys this change wrote, while the hostname is still its
				left = site_env.put_back(previous, values, existed, guard=site_env.HOSTNAME)
				if left:
					print(f"[NetRollout] undoing the hostname {new}: site.env's "
					      f"{', '.join(left)} changed again since; left as it is",
					      flush=True)
		return undo

	def apply(self, change: Callable[[], Undo], hostname: str | None = None) -> Verdict:
		"""Make a change nginx must accept and wait for its verdict - rejected:
		the change is undone.

		:param change: writes the files; returns its undo(). What it raises
		 propagates (it has changed nothing)
		:param hostname: wait for the site applied for this name (a new
		 hostname); None: any verdict
		:returns: the verdict - applied / rejected (undone), or why there is
		 none: not_managed (no NetRollout nginx reports here) or no_answer"""
		managed = self.managed
		started = time.time()
		undo = change()
		verdict = self._verdict(managed, started, hostname)
		if verdict.rejected:
			undo()
		return verdict

	@staticmethod
	def _verdict(managed: bool, started: float, hostname: str | None) -> Verdict:
		"""nginx's answer to a change written at `started` (with `hostname`:
		the answer for that hostname)."""
		if not managed:
			return Verdict(VerdictState.NOT_MANAGED)
		status = wait_for_status(started, hostname=hostname)
		if status is None:
			return Verdict(VerdictState.NO_ANSWER)
		return Verdict(status.get("state"), status.get("message"), status.get("time"))


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
