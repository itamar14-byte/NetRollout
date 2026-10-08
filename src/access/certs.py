"""TLS certificates for nginx: create a self-signed one, check an uploaded one.

Runs inside the app image, so the host needs no OpenSSL. Used by the
installer (`python -m src.access.certs selfsigned ...` through the image) and by the
Server Management certificate upload (`validate`).

The pair nginx serves is `fullchain.pem` + `privkey.pem` in the certs folder;
a `.selfsigned` marker beside them means NetRollout made it and may replace
it (e.g. for a new hostname) — an organisation's certificate is never touched.
"""
import argparse
import datetime
import ipaddress
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

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
	_write_atomic(out / KEY_FILE, key_pem, mode=0o600)
	_write_atomic(out / CERT_FILE, cert_pem, mode=0o644)
	_write_atomic(out / SELFSIGNED_MARKER,
	              f"{host}\n{now.isoformat()}\n".encode(), mode=0o644)
	return out


def is_selfsigned(cert_dir: str | Path | None = None) -> bool:
	"""Did NetRollout make the certificate in `cert_dir` (so it may replace
	it)? The certs folder when None."""
	return ((Path(cert_dir) if cert_dir else runtime.certs_dir())
	        / SELFSIGNED_MARKER).is_file()


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
	"""Write through a temp file in the same folder + rename: readers see the
	old file or the new one, never half of one."""
	fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
	try:
		with os.fdopen(fd, "wb") as f:
			f.write(data)
		os.chmod(tmp, mode)
		os.replace(tmp, path)
	except BaseException:
		if os.path.exists(tmp):
			os.remove(tmp)
		raise


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
