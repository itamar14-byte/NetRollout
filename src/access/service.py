"""How NetRollout is reached, as the web app changes it (app.access): the
hostname and the HTTPS port (System Settings) with nginx and the port helper
following them, and the certificate nginx serves. Built once at start
(launch_app); the routes keep the request parsing, the answers and the audit.

Its own module, not src/access/__init__.py: the setup core imports
src.access, and must not load this."""
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

from src.access import port as port_apply
from src.access.certs import CertCheck, CertificateStore, ProxyError, Undo
from src.access.nginx import Nginx, Verdict, server_ips, write_site
from src.db.settings import SETTINGS, Change, SettingsError, SettingsStore


# nginx (the hostname) and the port helper (the port) follow these settings:
# saved or reset through Access.save
FOLLOWED = ("public_hostname", "https_port")


@dataclass
class SaveResult:
	"""A save that went through: what changed, and nginx's verdict on a new
	hostname."""
	changes: list[Change] = field(default_factory=list)
	proxy: Verdict | None = None


class Access:
	"""The settings, the certs folder, nginx and the port request, changed
	together - all or nothing."""

	def __init__(self, settings: SettingsStore,
	             certificates: CertificateStore | None = None) -> None:
		""":param certificates: the certs folder; the app's when None"""
		self.settings = settings
		self.certificates = certificates or CertificateStore()
		self.nginx = Nginx(self.certificates)

	def save(self, values: dict[str, Any], actor: uuid.UUID | None) -> SaveResult:
		"""Validate, prepare nginx for a new hostname and the port helper for a
		new port, save, and wait for nginx's verdict — all or nothing: an
		invalid value, a certificate that doesn't cover the new name, a file
		that can't be written, or nginx rejecting the result leaves every
		setting and file as it was. (A new port is applied later, by the
		helper.)

		:param values: setting key → the value typed
		:param actor: who saves (the setting rows' author)
		:raises SettingsError: why, by setting"""
		changes = self.settings.plan(values)
		host = next((c for c in changes if c.key == "public_hostname"), None)
		port = next((c for c in changes if c.key == "https_port"), None)
		saved: list[Change] = []

		def change() -> Undo:
			undo_host = self._change_hostname(cast(str, host.new)) if host else None
			undo_port = self._request_port(cast(int, port.new), undo_host) if port else None
			try:
				saved.extend(self.settings.update(values, actor))
			except SettingsError:            # changed meanwhile: back out
				for back in (undo_host, undo_port):
					if back:
						back()
				raise

			def undo() -> None:
				# the whole save goes back, not only the hostname
				self.settings.update({c.key: c.old for c in saved}, actor)
				for back in (undo_host, undo_port):
					if back:
						back()
			return undo

		if not host:
			change()
			return SaveResult(saved)
		verdict = self.nginx.apply(change, cast(str, host.new))
		if verdict.rejected:
			raise SettingsError({"public_hostname":
				f"nginx rejected the new hostname: {verdict.message} — "
				f"nothing was changed, the previous hostname is back."})
		return SaveResult(saved, verdict)

	def _change_hostname(self, new: str) -> Undo:
		""":raises SettingsError: nginx can't serve it (Nginx.change_hostname)"""
		try:
			return self.nginx.change_hostname(new)
		except ProxyError as e:
			raise SettingsError({"public_hostname": str(e)}) from e

	@staticmethod
	def _request_port(new: int, undo_host: Undo | None) -> Undo:
		""":param undo_host: a new hostname's, undone when the request can't
		 be written
		:raises SettingsError: site.env can't be written"""
		try:
			return port_apply.request_port(new)
		except OSError as e:
			if undo_host:
				undo_host()
			raise SettingsError({"https_port":
				f"NetRollout couldn't write {e.filename or 'config/'}: "
				f"{e.strerror or e}. Nothing was changed."}) from e

	def reset(self, key: str, actor: uuid.UUID | None) -> SaveResult:
		"""A setting back to its default - the hostname and the port through
		save(), as nginx and the port helper follow them.

		:raises SettingsError: the result would break a rule (or as save())"""
		if key in FOLLOWED:
			return self.save({key: SETTINGS[key].default}, actor)
		change = self.settings.reset(key, actor)
		return SaveResult([change] if change else [])

	def overview(self) -> dict[str, Any]:
		""":returns: the Access card (Nginx.overview), for the saved hostname"""
		return self.nginx.overview(self.settings.get("public_hostname"))

	def port_state(self) -> dict[str, Any]:
		""":returns: where the saved port stands (port.state)"""
		return port_apply.state(self.settings.get("https_port"))

	def confirm_port(self, apply_id: str, reached_port: int) -> str | None:
		"""An admin's browser reached NetRollout on `reached_port` during the
		trial `apply_id` (port.confirm); nginx's redirects follow the confirmed
		port at once, not after the helper's next step.

		:returns: None when confirmed, else why it can't be"""
		problem = port_apply.confirm(apply_id, reached_port)
		if problem:
			return problem
		try:
			write_site(self.settings.get("public_hostname"))
		except (ValueError, OSError):
			pass                              # nginx keeps the previous values
		return None

	def retry_port(self) -> None:
		"""Ask the helper again for the saved port (a new request).

		:raises OSError: site.env can't be written"""
		port_apply.request_port(self.settings.get("https_port"))

	def upload_certificate(self, cert_pem: bytes, key_pem: bytes) -> tuple[CertCheck, Verdict]:
		"""An organisation's certificate and key, checked against the saved
		hostname and used (CertificateStore.install) - nginx rejecting them
		puts the previous ones back.

		:returns: (the check, nginx's verdict)
		:raises ProxyError: refused, having changed nothing"""
		hostname = self.settings.get("public_hostname")
		checked: list[CertCheck] = []

		def change() -> Undo:
			check, undo = self.certificates.install(cert_pem, key_pem, hostname)
			checked.append(check)
			return undo
		verdict = self.nginx.apply(change)
		return checked[0], verdict

	def generate_selfsigned(self) -> tuple[list[str], Verdict]:
		"""A new self-signed certificate for the saved hostname and the
		server's addresses (CertificateStore.generate_selfsigned) - nginx
		rejecting it puts the previous one back.

		:returns: (the names it covers, nginx's verdict)
		:raises ProxyError: no name to make it for, or it couldn't be written"""
		hostname = self.settings.get("public_hostname")
		names: list[str] = []

		def change() -> Undo:
			undo = self.certificates.generate_selfsigned(hostname, server_ips())
			names.extend((self.certificates.summary(hostname) or {}).get("names", []))
			return undo
		verdict = self.nginx.apply(change)
		return names, verdict
