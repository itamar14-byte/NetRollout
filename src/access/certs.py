"""TLS certificates for nginx: create a self-signed one, check an uploaded one,
and the certs folder that holds the one in use (CertificateStore).

Runs inside the app image, so the host needs no OpenSSL. Used by the
installer (`python -m src.access.certs selfsigned ...` through the image), the
setup core's checks and by the app (Server Management's certificate, a new
hostname, the upkeep that drops previous hostnames).

The pair nginx serves is `fullchain.pem` + `privkey.pem` in the certs folder;
a `.selfsigned` marker beside them means NetRollout made it and may replace
it (e.g. for a new hostname) — an organisation's certificate is never touched.
"""
import argparse
import datetime
import ipaddress
import json
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from src import runtime


CERT_FILE = "fullchain.pem"
KEY_FILE = "privkey.pem"
SELFSIGNED_MARKER = ".selfsigned"
# The longest validity Apple devices accept for a TLS server certificate, so
# trusting it by hand works on every client
SELFSIGNED_DAYS = 825
EXPIRY_WARNING_DAYS = 30
# A self-signed certificate reissued for a new hostname keeps the previous
# names this long, so people still typing one reach the redirect without a
# name warning — then they're dropped (the deadlines: OLD_NAMES_FILE).
OLD_NAMES_FILE = ".old-names.json"     # in the certs folder
NAME_TRANSITION_DAYS = 7
UPKEEP_INTERVAL_SECONDS = 3600

Undo = Callable[[], None]   # puts the previous files back

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
# a SAN's IP entry as cryptography types it (a network only in name constraints)
SanIP = IPAddress | ipaddress.IPv4Network | ipaddress.IPv6Network


# ── Self-signed ──────────────────────────────────────────────────────────────

def selfsigned(hostname: str, ips: Iterable[str] = (),
               out_dir: str | Path | None = None, days: int = SELFSIGNED_DAYS,
               also_names: Iterable[str] = ()) -> Path:
	"""Write a self-signed certificate (ECDSA P-256) and its key, plus the
	marker that says NetRollout made it.

	:param hostname: its name (the common name, and the first SAN)
	:param ips: addresses it's reached by, so https://<ip> works too
	:param out_dir: where; the certs folder when None
	:param also_names: more names it covers (a renamed server's old ones)
	:returns: the folder
	:raises ValueError: no hostname"""
	out = Path(out_dir) if out_dir else runtime.certs_dir()
	out.mkdir(parents=True, exist_ok=True)
	host = hostname.strip().rstrip(".").lower()
	if not host:
		raise ValueError("a hostname is required")

	names, seen = [], set()
	for value in (host, *also_names, *ips):
		ip = _ip(value)
		general = x509.IPAddress(ip) if ip else x509.DNSName(value)
		if general not in seen:
			seen.add(general)
			names.append(general)

	key = ec.generate_private_key(ec.SECP256R1())
	subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])])
	now = datetime.datetime.now(datetime.timezone.utc)
	cert = (x509.CertificateBuilder()
	        .subject_name(subject).issuer_name(subject)
	        .public_key(key.public_key())
	        .serial_number(x509.random_serial_number())
	        .not_valid_before(now - datetime.timedelta(minutes=5))  # clock skew
	        .not_valid_after(now + datetime.timedelta(days=days))
	        .add_extension(x509.SubjectAlternativeName(names), critical=False)
	        .add_extension(x509.BasicConstraints(ca=False, path_length=None),
	                       critical=True)
	        .add_extension(x509.ExtendedKeyUsage(
		        [ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
	        .add_extension(x509.SubjectKeyIdentifier.from_public_key(
		        key.public_key()), critical=False)
	        .sign(key, hashes.SHA256()))

	key_pem = key.private_bytes(serialization.Encoding.PEM,
	                            serialization.PrivateFormat.PKCS8,
	                            serialization.NoEncryption())
	cert_pem = cert.public_bytes(serialization.Encoding.PEM)
	# Key first: nginx's watcher tests the pair before using it, so a moment
	# with the new key and the old certificate is rejected, never served
	runtime.write_atomic(out / KEY_FILE, key_pem, mode=0o600)
	runtime.write_atomic(out / CERT_FILE, cert_pem, mode=0o644)
	runtime.write_atomic(out / SELFSIGNED_MARKER,
	                     f"{host}\n{now.isoformat()}\n".encode(), mode=0o644)
	return out


def is_selfsigned(cert_dir: str | Path | None = None) -> bool:
	"""Did NetRollout make the certificate in `cert_dir` (so it may replace
	it)? The certs folder when None."""
	return ((Path(cert_dir) if cert_dir else runtime.certs_dir())
	        / SELFSIGNED_MARKER).is_file()


# ── Validation ───────────────────────────────────────────────────────────────

@dataclass
class CertCheck:
	"""What `validate` found: `problems` stop the certificate from being used;
	`warnings` don't. The details are for the page that shows it."""
	problems: list[str] = field(default_factory=list)
	warnings: list[str] = field(default_factory=list)
	subject: str = ""
	names: list[str] = field(default_factory=list)
	not_after: datetime.datetime | None = None

	@property
	def ok(self) -> bool:
		""":returns: whether it may be used (no problems)"""
		return not self.problems


def validate(cert_pem: bytes, key_pem: bytes, hostname: str | None = None,
             now: datetime.datetime | None = None) -> CertCheck:
	"""Check a certificate and its private key before nginx is given them:
	the key matches and isn't password-protected, the dates, the names (and
	that they cover `hostname`), the chain's order.

	:param cert_pem: the server's certificate first, then any intermediates
	:param hostname: the name it must cover; None: not checked
	:param now: the time to check the dates against (tests); now when None"""
	check = CertCheck()
	now = now or datetime.datetime.now(datetime.timezone.utc)

	try:
		chain = x509.load_pem_x509_certificates(cert_pem)
	except ValueError:
		check.problems.append("The certificate file isn't a PEM certificate.")
		chain = []
	try:
		key = serialization.load_pem_private_key(key_pem, password=None)
	except TypeError:
		check.problems.append("The private key is password-protected — nginx "
		                      "can't use it unattended. Export it without a "
		                      "password.")
		key = None
	except (ValueError, UnsupportedAlgorithm):
		check.problems.append("The key file isn't a PEM private key.")
		key = None
	if not chain:
		return check

	leaf = chain[0]
	check.subject = leaf.subject.rfc4514_string()
	check.not_after = leaf.not_valid_after_utc
	dns_names, ips = _san(leaf)
	check.names = dns_names + [str(ip) for ip in ips]

	if key is not None and not _same_key(leaf, key):
		check.problems.append("The private key doesn't belong to this "
		                      "certificate (or the server's certificate isn't "
		                      "first in the file).")
	if now < leaf.not_valid_before_utc:
		check.problems.append(f"The certificate isn't valid yet (from "
		                      f"{leaf.not_valid_before_utc:%Y-%m-%d}).")
	if now > leaf.not_valid_after_utc:
		check.problems.append(f"The certificate expired on "
		                      f"{leaf.not_valid_after_utc:%Y-%m-%d}.")
	elif leaf.not_valid_after_utc - now < datetime.timedelta(
			days=EXPIRY_WARNING_DAYS):
		check.warnings.append(f"The certificate expires on "
		                      f"{leaf.not_valid_after_utc:%Y-%m-%d}.")

	if not check.names:
		check.problems.append("The certificate has no subject alternative "
		                      "names — browsers ignore the common name.")
	elif hostname and not host_matches(hostname, dns_names, ips):
		check.problems.append(f"The certificate doesn't cover {hostname} "
		                      f"(it covers: {', '.join(check.names)}).")

	for child, parent in zip(chain, chain[1:]):
		if child.issuer != parent.subject:
			check.warnings.append("The chain isn't in order: the server's "
			                      "certificate first, then each issuer.")
			break
	return check


def names_in(cert_pem: bytes) -> tuple[list[str], list[SanIP]]:
	"""(DNS names, IP addresses) the server certificate (the first in the
	file) covers."""
	return _san(x509.load_pem_x509_certificates(cert_pem)[0])


def common_name(cert_pem: bytes) -> str:
	""":returns: the server certificate's common name ("" without one) - the
	 hostname a self-signed one was issued to"""
	cert = x509.load_pem_x509_certificates(cert_pem)[0]
	names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
	return str(names[0].value) if names else ""


def host_matches(hostname: str, dns_names: Iterable[str],
                 ips: Sequence[SanIP]) -> bool:
	"""Browser rules: an IP must be listed as an IP; a wildcard covers
	exactly one label, and only as the whole left-most label."""
	host = hostname.strip().rstrip(".").lower()
	ip = _ip(host)
	if ip:
		return ip in ips
	for name in dns_names:
		name = name.rstrip(".").lower()
		if name == host:
			return True
		if name.startswith("*.") and "." in host:
			label, rest = host.split(".", 1)
			if label and rest == name[2:]:
				return True
	return False


def _san(cert: x509.Certificate) -> tuple[list[str], list[SanIP]]:
	""":returns: (DNS names, IP addresses) of its subject alternative names"""
	try:
		san = cert.extensions.get_extension_for_class(
			x509.SubjectAlternativeName).value
	except x509.ExtensionNotFound:
		return [], []
	return (san.get_values_for_type(x509.DNSName),
	        san.get_values_for_type(x509.IPAddress))


def _same_key(cert: x509.Certificate, key: PrivateKeyTypes) -> bool:
	""":returns: whether the key is the certificate's (same public key)"""
	spki = (serialization.Encoding.DER,
	        serialization.PublicFormat.SubjectPublicKeyInfo)
	return (cert.public_key().public_bytes(*spki)
	        == key.public_key().public_bytes(*spki))


def _ip(value: str) -> IPAddress | None:
	""":returns: the address; None when it isn't one (a hostname)"""
	try:
		return ipaddress.ip_address(value.strip())
	except ValueError:
		return None


# ── The certificate in use: the certs folder ─────────────────────────────────

class ProxyError(Exception):
	"""A change of what nginx serves (its certificate, its hostname) couldn't
	be made; the message is for the page. Nothing was left changed."""


class CertificateKind(StrEnum):
	"""Whose the certificate in use is - what NetRollout may do to it."""
	SELF_SIGNED = "self-signed"       # NetRollout made it: reissued as needed
	ORGANISATION = "organisation"     # never touched


class Certificate(ABC):
	"""The certificate in the certs folder (CertificateStore.current()). Its
	kind decides what a new hostname and the upkeep do to it. Called under
	the store's lock, with its snapshot taken."""
	kind: ClassVar[CertificateKind]

	def __init__(self, store: "CertificateStore") -> None:
		self._store = store

	def pem(self) -> bytes:
		""":returns: the certificate file (the server's first)
		:raises OSError: it can't be read"""
		return (self._store.folder() / CERT_FILE).read_bytes()

	@abstractmethod
	def rename_to(self, new: str) -> None:
		"""Make it serve the hostname `new`.

		:raises ProxyError: it can't (the reason, for the page)
		:raises OSError: a file can't be read or written
		:raises ValueError: the certificate isn't readable PEM"""

	@abstractmethod
	def drop_expired(self, now: float) -> list[str]:
		"""Drop the previous hostnames whose transition ended (`now`, epoch
		seconds).

		:returns: the names dropped"""


class SelfSigned(Certificate):
	"""NetRollout's own: reissued for a new hostname, keeping the addresses
	it covered and its previous names for NAME_TRANSITION_DAYS."""
	kind = CertificateKind.SELF_SIGNED

	def rename_to(self, new: str) -> None:
		dns, ips = names_in(self.pem())
		# the names it covered stay for a transition period
		now = time.time()
		old = {n: u for n, u in self._store.old_names().items() if u > now}
		for name in dns:
			old.setdefault(name, now + NAME_TRANSITION_DAYS * 86400)
		old.pop(new, None)
		selfsigned(new, [str(ip) for ip in ips], self._store.folder(),
		           also_names=sorted(old))
		self._store.keep_old_names(old)

	def drop_expired(self, now: float) -> list[str]:
		stored = self._store.old_names()
		if not stored:
			return []
		keep = {n: u for n, u in stored.items() if u > now}
		pem = self.pem()
		dns, ips = names_in(pem)
		# the hostname is the common name (an IP address isn't among the DNS names)
		host = common_name(pem)
		others = [n for n in dns if n != host]
		expired = [n for n in others if n in stored and n not in keep]
		if expired:
			selfsigned(host, [str(ip) for ip in ips], self._store.folder(),
			           also_names=[n for n in others if n not in expired])
		self._store.keep_old_names(keep)
		return expired


class Organisation(Certificate):
	"""An organisation's: never reissued - a new hostname must already be
	covered by it."""
	kind = CertificateKind.ORGANISATION

	def rename_to(self, new: str) -> None:
		dns, ips = names_in(self.pem())
		if not host_matches(new, dns, ips):
			covers = ", ".join([*dns, *map(str, ips)]) or "no names"
			raise ProxyError(
				f"The certificate in use covers {covers} — not {new}. Upload "
				f"a certificate for {new} first (Server Management → "
				f"Certificate), then change the hostname.")

	def drop_expired(self, now: float) -> list[str]:
		return []


class CertificateStore:
	"""The certs folder: the pair nginx serves, the self-signed marker and the
	previous names' deadlines. Every change runs under one lock, from a
	snapshot of those files - all or nothing - and its undo puts back only
	what no later change has replaced."""
	# one per process, like the folder: a hostname save and the upkeep thread
	# never reissue at the same moment (re-entrant: a hostname change holds it
	# across the certificate and site.env - Nginx.change_hostname)
	lock = threading.RLock()

	@staticmethod
	def folder() -> Path:
		return runtime.certs_dir()

	@property
	def selfsigned(self) -> bool:
		""":returns: whether NetRollout made the certificate in use (its marker)"""
		return is_selfsigned(self.folder())

	def current(self) -> Certificate | None:
		""":returns: the certificate in use, by its kind; None: no file"""
		folder = self.folder()
		if not (folder / CERT_FILE).is_file():
			return None
		return SelfSigned(self) if is_selfsigned(folder) else Organisation(self)

	def check(self, hostname: str | None, require_key: bool = True) -> CertCheck:
		"""The certificate in use and its key, validated against `hostname`
		(validate).

		:param require_key: an unreadable key raises; else it's checked as
		 missing (validate reports it)
		:raises FileNotFoundError: there is no certificate
		:raises OSError: it - or a required key - can't be read"""
		folder = self.folder()
		cert_pem = (folder / CERT_FILE).read_bytes()
		try:
			key_pem = (folder / KEY_FILE).read_bytes()
		except OSError:
			if require_key:
				raise
			key_pem = b""
		return validate(cert_pem, key_pem, hostname or None)

	def summary(self, hostname: str | None) -> dict[str, Any] | None:
		"""What the Access card shows about the certificate, checked against
		`hostname` (the saved one): None (no file) | {"names", "not_after",
		"selfsigned", "problems", "warnings", "old_names": [{"name", "until"}]}"""
		try:
			check = self.check(hostname, require_key=False)
		except FileNotFoundError:
			return None
		except OSError as e:
			return {"names": [], "not_after": None, "selfsigned": False, "old_names": [],
			        "problems": [f"NetRollout can't read the certificate: {e.strerror or e}."],
			        "warnings": []}
		old = self.old_names()
		return {
			"names": check.names,
			"not_after": check.not_after.isoformat() if check.not_after else None,
			"selfsigned": self.selfsigned,
			"problems": check.problems, "warnings": check.warnings,
			"old_names": [{"name": n, "until": old[n]} for n in check.names if n in old]}

	def rename(self, new: str) -> Undo:
		"""Serve the hostname `new` (empty: nothing to do): the certificate in
		use renamed by its kind (Certificate.rename_to).

		:returns: undo()
		:raises ProxyError: it can't (an organisation's that doesn't cover it)
		:raises OSError: a file can't be read or written
		:raises ValueError: the certificate isn't readable PEM - each having
		 changed nothing"""
		with self.lock:
			saved = self._snapshot()
			try:
				cert = self.current() if new else None
				if cert:
					cert.rename_to(new)
			except (ProxyError, OSError, ValueError):
				self._restore(saved)
				raise
			return self._undo(saved, f"undoing the hostname {new}")

	def install(self, cert_pem: bytes, key_pem: bytes,
	            hostname: str | None) -> tuple[CertCheck, Undo]:
		"""Use an organisation's certificate + key: checked first (validate,
		against the saved `hostname`), then written key first (nginx's watcher
		tests the pair before using it). The self-signed marker goes, so
		NetRollout never reissues it.

		:returns: (the check - its warnings for the page, undo)
		:raises ProxyError: every problem found, having changed nothing"""
		check = validate(cert_pem, key_pem, hostname or None)
		if not check.ok:
			raise ProxyError(" ".join(check.problems))
		with self.lock:
			folder = self.folder()
			saved = self._snapshot()
			try:
				folder.mkdir(parents=True, exist_ok=True)
				self._restore({folder / KEY_FILE: key_pem})
				self._restore({folder / CERT_FILE: cert_pem})
				(folder / SELFSIGNED_MARKER).unlink(missing_ok=True)
				(folder / OLD_NAMES_FILE).unlink(missing_ok=True)
			except OSError as e:
				self._restore(saved)
				raise ProxyError(f"NetRollout couldn't write {e.filename or folder}: "
				                 f"{e.strerror or e}. Nothing was changed.") from e
			return check, self._undo(saved, "undoing the certificate upload")

	def generate_selfsigned(self, hostname: str | None, server_ips: list[str]) -> Undo:
		"""Replace the certificate with a new self-signed one for `hostname`
		(else the name the current certificate is for), for `server_ips` and
		any address the current one covers.

		:returns: undo()
		:raises ProxyError: no name to make it for, or it couldn't be written -
		 having changed nothing"""
		with self.lock:
			folder = self.folder()
			cert = folder / CERT_FILE
			dns: list[str] = []
			ips: list[SanIP] = []
			try:
				if cert.is_file():
					dns, ips = names_in(cert.read_bytes())
			except (OSError, ValueError):
				pass                          # unreadable: start from the hostname
			name = hostname or (dns[0] if dns else "")
			if not name:
				raise ProxyError("Set the hostname first (System Settings → Access): "
				                 "the certificate is made for it.")
			saved = self._snapshot()
			try:
				addresses = list(server_ips)
				addresses += [str(ip) for ip in ips if str(ip) not in addresses]
				selfsigned(name, addresses, folder)
				(folder / OLD_NAMES_FILE).unlink(missing_ok=True)
			except (OSError, ValueError) as e:
				self._restore(saved)
				where = getattr(e, "filename", None) or folder
				raise ProxyError(f"NetRollout couldn't write {where}: "
				                 f"{getattr(e, 'strerror', None) or e}. Nothing was "
				                 f"changed.") from e
			return self._undo(saved, "undoing the self-signed certificate")

	def drop_expired(self, now: float | None = None) -> list[str]:
		"""Drop the previous hostnames whose transition period ended from
		NetRollout's self-signed certificate (reissued; nginx reloads it, no
		restart). An organisation's certificate is never touched.

		:param now: the time (epoch seconds; tests); now when None
		:returns: the names dropped"""
		with self.lock:
			cert = self.current()
			if cert is None:
				return []
			return cert.drop_expired(time.time() if now is None else now)

	def old_names(self) -> dict[str, float]:
		"""{name: until (epoch seconds)}; nothing readable → {}."""
		try:
			data = json.loads((self.folder() / OLD_NAMES_FILE).read_text(encoding="utf-8"))
		except (OSError, ValueError):
			return {}
		if not isinstance(data, dict):
			return {}
		return {n: float(u) for n, u in data.items()
		        if isinstance(n, str) and isinstance(u, (int, float))}

	def keep_old_names(self, names: dict[str, float]) -> None:
		"""Keep the previous names' deadlines ({name: until}); none: the file goes."""
		path = self.folder() / OLD_NAMES_FILE
		if not names:
			path.unlink(missing_ok=True)
			return
		self._restore({path: json.dumps(names, indent=1, sort_keys=True).encode()})

	def _snapshot(self, paths: list[Path] | None = None) -> dict[Path, bytes | None]:
		""":param paths: the files; every file a change replaces when None
		:returns: each file's content (None: it doesn't exist), for _restore"""
		if paths is None:
			folder = self.folder()
			paths = [folder / CERT_FILE, folder / KEY_FILE,
			         folder / SELFSIGNED_MARKER, folder / OLD_NAMES_FILE]
		return {p: (p.read_bytes() if p.is_file() else None) for p in paths}

	@staticmethod
	def _restore(saved: dict[Path, bytes | None]) -> None:
		"""Write each file's content atomically (the key owner-only); None
		deletes it - a _snapshot put back, or new files written."""
		for path, data in saved.items():
			if data is None:
				path.unlink(missing_ok=True)
				continue
			runtime.write_atomic(path, data, 0o600 if path.name == KEY_FILE else 0o644)

	def _undo(self, saved: dict[Path, bytes | None], what: str) -> Undo:
		"""undo() for a change that has just written its files (called under
		the lock, right after): `saved` is put back only while the files are
		still the ones the change wrote - a change made since (another upload,
		a reissue) is left in place, and the log says so."""
		written = self._snapshot(list(saved))

		def undo() -> None:
			with self.lock:
				if self._snapshot(list(written)) != written:
					print(f"[NetRollout] {what}: the certificate files weren't put back - "
					      f"they were changed again since; the newer ones stay", flush=True)
					return
				self._restore(saved)
		return undo


class CertificateUpkeep(runtime.PeriodicTask):
	"""CertificateStore.drop_expired() now and every UPKEEP_INTERVAL_SECONDS -
	a server that never restarts still drops them."""
	FAILURE = "certificate upkeep failed: {error}"

	def __init__(self, store: CertificateStore) -> None:
		super().__init__("certificate-upkeep", UPKEEP_INTERVAL_SECONDS)
		self._store = store

	def run_once(self) -> None:
		dropped = self._store.drop_expired()
		if dropped:
			print(f"[NetRollout] certificate reissued without the previous "
			      f"hostname(s) {', '.join(dropped)} (transition of "
			      f"{NAME_TRANSITION_DAYS} days over)", flush=True)


# ── Command line (the installer runs it through the image) ───────────────────

def main(argv: list[str] | None = None) -> int:
	"""`selfsigned` writes a certificate; `validate` prints its problems and
	warnings.

	:param argv: the arguments; sys.argv's when None
	:returns: the exit code: 0 done / valid, 1 not"""
	parser = argparse.ArgumentParser(
		prog="python -m src.access.certs",
		description="Create or check the TLS certificate nginx serves.")
	sub = parser.add_subparsers(dest="action", required=True)
	make = sub.add_parser("selfsigned", help="create a self-signed certificate")
	make.add_argument("--host", required=True, help="the server's hostname")
	make.add_argument("--ip", action="append", default=[],
	                  help="an address it's reached by (repeatable)")
	make.add_argument("--out", help="folder (default: the certs folder)")
	check = sub.add_parser("validate", help="check a certificate + key")
	check.add_argument("--cert", required=True)
	check.add_argument("--key", required=True)
	check.add_argument("--host", help="the hostname it must cover")
	args = parser.parse_args(argv)

	if args.action == "selfsigned":
		for ip in args.ip:
			if not _ip(ip):
				print(f"Not an IP address: {ip}", file=sys.stderr)
				return 1
		out = selfsigned(args.host, args.ip, args.out)
		print(f"Self-signed certificate for {', '.join([args.host, *args.ip])} "
		      f"written to {out}")
		return 0

	try:
		result = validate(Path(args.cert).read_bytes(),
		                  Path(args.key).read_bytes(), args.host)
	except OSError as e:
		print(f"Can't read {e.filename}: {e.strerror}", file=sys.stderr)
		return 1
	for problem in result.problems:
		print(f"PROBLEM: {problem}")
	for warning in result.warnings:
		print(f"WARNING: {warning}")
	if result.subject:
		print(f"Subject: {result.subject}\nNames:   {', '.join(result.names)}"
		      f"\nExpires: {result.not_after:%Y-%m-%d}")
	return 0 if result.ok else 1

if __name__ == "__main__":
	sys.exit(main())
