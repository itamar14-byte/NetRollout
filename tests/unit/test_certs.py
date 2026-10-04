"""src/certs.py: the self-signed certificate nginx starts with, and the checks
an uploaded certificate passes before nginx gets it. Certificates are built
here — no files from outside, no network."""
import datetime
import ipaddress
import os
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from src import certs

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC)


def make_cert(names=("nr.corp.local",), ips=(), issuer=None, ca=False,
              not_before=None, not_after=None, key=None):
	"""A certificate signed by `issuer` (cert, key) or by itself."""
	key = key or ec.generate_private_key(ec.SECP256R1())
	subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,
	                                        names[0] if names else "x")])
	sign_cert, sign_key = issuer or (None, key)
	sans = [x509.DNSName(n) for n in names] + \
	       [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
	builder = (x509.CertificateBuilder()
	           .subject_name(subject)
	           .issuer_name(sign_cert.subject if sign_cert else subject)
	           .public_key(key.public_key())
	           .serial_number(x509.random_serial_number())
	           .not_valid_before(not_before or NOW - datetime.timedelta(days=1))
	           .not_valid_after(not_after or NOW + datetime.timedelta(days=365))
	           .add_extension(x509.BasicConstraints(ca=ca, path_length=None),
	                          critical=True))
	if sans:
		builder = builder.add_extension(x509.SubjectAlternativeName(sans),
		                                critical=False)
	return builder.sign(sign_key, hashes.SHA256()), key


def pem(cert):
	return cert.public_bytes(serialization.Encoding.PEM)


def key_pem(key, password=None):
	enc = (serialization.BestAvailableEncryption(password) if password
	       else serialization.NoEncryption())
	return key.private_bytes(serialization.Encoding.PEM,
	                         serialization.PrivateFormat.PKCS8, enc)


# ── Self-signed ──────────────────────────────────────────────────────────────

def test_selfsigned_covers_the_hostname_and_its_ips(tmp_path):
	certs.selfsigned("NR01.corp.local.", ["10.1.1.5", "fe80::1"], tmp_path)
	cert_pem = (tmp_path / certs.CERT_FILE).read_bytes()
	check = certs.validate(cert_pem, (tmp_path / certs.KEY_FILE).read_bytes(),
	                       "nr01.corp.local")
	assert check.ok and not check.warnings
	assert check.names == ["nr01.corp.local", "10.1.1.5", "fe80::1"]
	for host in ("10.1.1.5", "fe80::1"):
		assert certs.validate(cert_pem, (tmp_path / certs.KEY_FILE).read_bytes(),
		                      host).ok
	cert = x509.load_pem_x509_certificate(cert_pem)
	days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
	assert days == certs.SELFSIGNED_DAYS   # Apple's maximum
	assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)


def test_selfsigned_writes_the_marker_and_nothing_else(tmp_path):
	certs.selfsigned("nr01", out_dir=tmp_path)
	assert certs.is_selfsigned(tmp_path)
	assert sorted(p.name for p in tmp_path.iterdir()) == \
	       sorted([certs.CERT_FILE, certs.KEY_FILE, certs.SELFSIGNED_MARKER])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_selfsigned_key_is_private(tmp_path):
	certs.selfsigned("nr01", out_dir=tmp_path)
	assert (os.stat(tmp_path / certs.KEY_FILE).st_mode & 0o777) == 0o600


def test_selfsigned_replaces_its_own_pair(tmp_path):
	certs.selfsigned("old-name", out_dir=tmp_path)
	certs.selfsigned("new-name", out_dir=tmp_path)
	check = certs.validate((tmp_path / certs.CERT_FILE).read_bytes(),
	                       (tmp_path / certs.KEY_FILE).read_bytes(), "new-name")
	assert check.ok and check.names == ["new-name"]


def test_selfsigned_defaults_to_the_certs_folder(tmp_path, monkeypatch):
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	certs.selfsigned("nr01")
	assert certs.is_selfsigned()
	assert (tmp_path / "certs" / certs.CERT_FILE).is_file()


def test_selfsigned_needs_a_hostname(tmp_path):
	with pytest.raises(ValueError):
		certs.selfsigned("  ", out_dir=tmp_path)


def test_an_organisation_certificate_has_no_marker(tmp_path):
	cert, key = make_cert()
	(tmp_path / certs.CERT_FILE).write_bytes(pem(cert))
	assert not certs.is_selfsigned(tmp_path)


# ── Validation ───────────────────────────────────────────────────────────────

def test_an_organisation_chain_is_accepted():
	ca, ca_key = make_cert(names=(), ca=True)
	leaf, key = make_cert(names=("nr.corp.local",), issuer=(ca, ca_key))
	check = certs.validate(pem(leaf) + pem(ca), key_pem(key), "nr.corp.local")
	assert check.ok and not check.warnings
	assert check.subject == "CN=nr.corp.local"
	assert check.not_after == leaf.not_valid_after_utc


def test_a_chain_out_of_order_is_a_warning_or_a_key_mismatch():
	ca, ca_key = make_cert(names=(), ca=True)
	leaf, key = make_cert(issuer=(ca, ca_key))
	# the issuer first: the key no longer matches the first certificate
	check = certs.validate(pem(ca) + pem(leaf), key_pem(key))
	assert any("doesn't belong" in p for p in check.problems)
	# an unrelated certificate after the server's: usable, but flagged
	other, _ = make_cert(names=("other",))
	check = certs.validate(pem(leaf) + pem(other), key_pem(key))
	assert check.ok and any("isn't in order" in w for w in check.warnings)


def test_a_key_from_another_certificate_is_rejected():
	cert, _ = make_cert()
	_, other_key = make_cert()
	check = certs.validate(pem(cert), key_pem(other_key))
	assert not check.ok and "doesn't belong" in check.problems[0]


def test_a_password_protected_key_is_rejected():
	cert, key = make_cert()
	check = certs.validate(pem(cert), key_pem(key, password=b"secret"))
	assert not check.ok and "password-protected" in check.problems[0]


def test_files_that_are_not_pem_are_rejected():
	check = certs.validate(b"not a certificate", b"not a key")
	assert len(check.problems) == 2
	assert "isn't a PEM certificate" in check.problems[0]
	assert "isn't a PEM private key" in check.problems[1]


def test_expired_and_not_yet_valid_are_rejected():
	old, key = make_cert(not_before=NOW - datetime.timedelta(days=400),
	                     not_after=NOW - datetime.timedelta(days=1))
	check = certs.validate(pem(old), key_pem(key))
	assert not check.ok and "expired" in check.problems[0]
	future, key = make_cert(not_before=NOW + datetime.timedelta(days=1))
	check = certs.validate(pem(future), key_pem(key))
	assert not check.ok and "isn't valid yet" in check.problems[0]


def test_expiring_soon_is_only_a_warning():
	cert, key = make_cert(not_after=NOW + datetime.timedelta(days=10))
	check = certs.validate(pem(cert), key_pem(key))
	assert check.ok and "expires on" in check.warnings[0]


def test_a_certificate_without_alternative_names_is_rejected():
	cert, key = make_cert(names=())
	check = certs.validate(pem(cert), key_pem(key), "x")
	assert not check.ok and "no subject alternative names" in check.problems[0]


def test_a_hostname_it_does_not_cover_is_rejected():
	cert, key = make_cert(names=("nr.corp.local",))
	check = certs.validate(pem(cert), key_pem(key), "netrollout.corp.local")
	assert not check.ok
	assert check.problems[0] == ("The certificate doesn't cover "
	                             "netrollout.corp.local (it covers: "
	                             "nr.corp.local).")


@pytest.mark.parametrize("host,expected", [
	("nr.corp.local", True),
	("NR.Corp.Local.", True),       # case and a trailing dot don't matter
	("a.b.corp.local", False),      # a wildcard covers one label
	("corp.local", False),          # ...and at least one
	("nr.other.local", False),
	("10.0.0.5", True),             # an IP must be listed as an IP
	("10.0.0.6", False),
])
def test_host_matching_follows_browser_rules(host, expected):
	assert certs.host_matches(host, ["*.corp.local"],
	                          [ipaddress.ip_address("10.0.0.5")]) is expected


def test_an_ip_written_as_a_dns_name_does_not_cover_the_ip():
	assert not certs.host_matches("10.0.0.5", ["10.0.0.5"], [])


# ── Command line ─────────────────────────────────────────────────────────────

def test_cli_selfsigned_then_validate(tmp_path, capsys):
	assert certs.main(["selfsigned", "--host", "nr01", "--ip", "10.1.1.5",
	                   "--out", str(tmp_path)]) == 0
	assert certs.main(["validate", "--cert", str(tmp_path / certs.CERT_FILE),
	                   "--key", str(tmp_path / certs.KEY_FILE),
	                   "--host", "10.1.1.5"]) == 0
	out = capsys.readouterr().out
	assert "Names:   nr01, 10.1.1.5" in out


def test_cli_reports_problems_and_fails(tmp_path, capsys):
	cert, key = make_cert(names=("nr.corp.local",))
	(tmp_path / "c.pem").write_bytes(pem(cert))
	(tmp_path / "k.pem").write_bytes(key_pem(key))
	assert certs.main(["validate", "--cert", str(tmp_path / "c.pem"),
	                   "--key", str(tmp_path / "k.pem"), "--host", "other"]) == 1
	assert "PROBLEM: The certificate doesn't cover other" in \
	       capsys.readouterr().out
	assert certs.main(["validate", "--cert", str(tmp_path / "missing.pem"),
	                   "--key", str(tmp_path / "k.pem")]) == 1


def test_cli_rejects_a_bad_ip(tmp_path):
	assert certs.main(["selfsigned", "--host", "nr01", "--ip", "nr02",
	                   "--out", str(tmp_path)]) == 1
	assert not any(tmp_path.iterdir())


def test_extra_names_follow_the_hostname(tmp_path):
	certs.selfsigned("new.lab", ["10.1.1.5"], tmp_path, also_names=["old.lab"])
	dns, ips = certs.names_in((tmp_path / certs.CERT_FILE).read_bytes())
	assert dns == ["new.lab", "old.lab"] and [str(i) for i in ips] == ["10.1.1.5"]
