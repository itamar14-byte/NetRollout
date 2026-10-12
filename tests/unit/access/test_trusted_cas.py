"""The trusted CA certificates (certs.TrustedCAs): what an upload may hold,
duplicates, removing, the page's rows - and that a backup carries the file.
Certificates are built here (test_certs' helpers)."""
import datetime

import pytest

from src.access import certs
from src.backup import archive
from tests.unit.access.test_certs import NOW, key_pem, make_cert, pem


def store(tmp_path):
	return certs.TrustedCAs(tmp_path)


def test_one_or_several_certificates_are_added_once(tmp_path):
	"""A bundle of two adds both (in order); adding one of them again adds
	nothing; the file holds each once and the bundle reads back as PEM."""
	root, root_key = make_cert(("Corp Root CA",), ca=True)
	inter, _ = make_cert(("Corp Issuing CA",), ca=True, issuer=(root, root_key))
	trusted = store(tmp_path)
	added = trusted.add(pem(root) + pem(inter))
	assert [a.subject for a in added] == ["CN=Corp Root CA", "CN=Corp Issuing CA"]
	assert trusted.add(pem(inter)) == []
	assert [e.subject for e in trusted.entries()] == ["CN=Corp Root CA", "CN=Corp Issuing CA"]
	assert trusted.pem().count("BEGIN CERTIFICATE") == 2
	assert (tmp_path / certs.TRUSTED_CA_FILE).is_file()


@pytest.mark.parametrize("data,words", [
	(b"hello", "isn't a PEM certificate"),
	(b"", "isn't a PEM certificate"),
])
def test_what_isnt_a_certificate_is_refused(tmp_path, data, words):
	"""Text that isn't a PEM certificate is refused in words; no file."""
	with pytest.raises(certs.CertificateRefused, match=words):
		store(tmp_path).add(data)
	assert not (tmp_path / certs.TRUSTED_CA_FILE).exists()


def test_a_private_key_is_refused_even_next_to_a_certificate(tmp_path):
	"""A file holding a key - alone or with a certificate - is refused
	("never a key"), nothing written."""
	cert, key = make_cert(("Corp Root CA",), ca=True)
	with pytest.raises(certs.CertificateRefused, match="private key"):
		store(tmp_path).add(pem(cert) + key_pem(key))
	assert store(tmp_path).entries() == []


def test_an_expired_certificate_is_refused(tmp_path):
	"""An expired certificate can't anchor anything: refused, nothing added
	(the others in the file neither)."""
	good, _ = make_cert(("Good CA",), ca=True)
	old, _ = make_cert(("Old CA",), ca=True, not_before=NOW - datetime.timedelta(days=30),
	                   not_after=NOW - datetime.timedelta(days=1))
	with pytest.raises(certs.CertificateRefused, match="expired on"):
		store(tmp_path).add(pem(good) + pem(old))
	assert store(tmp_path).entries() == []


def test_a_servers_own_self_signed_certificate_is_allowed(tmp_path):
	"""A lab directory's self-signed server certificate (not a CA) is its
	own anchor: added, and the page's row says self-signed."""
	server, _ = make_cert(("ldap.lab.local",))
	(entry,) = store(tmp_path).add(pem(server))
	assert entry.as_dict()["self_signed"] is True


def test_a_servers_certificate_issued_by_a_ca_is_refused(tmp_path):
	"""The common mistake: the directory's own certificate (not a CA, issued by
	the organisation's CA) uploaded instead of the CA. It can't anchor
	anything: refused in words naming the CA to upload, nothing added (the
	CA next to it neither)."""
	root, root_key = make_cert(("Corp Root CA",), ca=True)
	server, _ = make_cert(("ldap.corp.local",), issuer=(root, root_key))
	with pytest.raises(certs.CertificateRefused,
	                   match=r"CN=ldap\.corp\.local is a server's own certificate, not a CA\. "
	                         r"Upload the CA that issued it \(CN=Corp Root CA\)"):
		store(tmp_path).add(pem(root) + pem(server))
	assert store(tmp_path).entries() == []


def test_removing_by_fingerprint_and_the_last_removes_the_file(tmp_path):
	"""Remove takes one out by its fingerprint; an unknown one is a
	LookupError; removing the last deletes the file."""
	a, _ = make_cert(("A CA",), ca=True)
	b, _ = make_cert(("B CA",), ca=True)
	trusted = store(tmp_path)
	first, second = trusted.add(pem(a) + pem(b))
	assert trusted.remove(first.fingerprint).subject == "CN=A CA"
	assert [e.subject for e in trusted.entries()] == ["CN=B CA"]
	with pytest.raises(LookupError):
		trusted.remove(first.fingerprint)
	trusted.remove(second.fingerprint)
	assert not (tmp_path / certs.TRUSTED_CA_FILE).exists()
	assert trusted.pem() is None


def test_the_page_warns_of_expiry(tmp_path):
	"""A row warns when the certificate expires within 30 days, and when it
	has expired (checked at a later time)."""
	soon, _ = make_cert(("Soon CA",), ca=True, not_after=NOW + datetime.timedelta(days=10))
	(entry,) = store(tmp_path).add(pem(soon))
	assert entry.as_dict()["warning"].startswith("Expires on ")
	later = NOW + datetime.timedelta(days=11)
	assert entry.as_dict(later)["warning"].startswith("Expired on ")
	far, _ = make_cert(("Far CA",), ca=True)
	(other,) = store(tmp_path).add(pem(far))
	assert other.as_dict()["warning"] is None


def test_backups_carry_the_trusted_certificates():
	"""The certs folder's trusted-ca.pem is among the files a backup carries
	(and a restore puts back)."""
	assert certs.TRUSTED_CA_FILE in archive.CERT_FILES
