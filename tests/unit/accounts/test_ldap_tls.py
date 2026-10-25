"""LDAPS verification (accounts.ldap): the TLS context always requires the
certificate and the host name and adds the trusted CA certificates to the
operating system's; a refused certificate is said in words. The real
handshakes are in tests/integration/test_ldaps.py."""
import ssl
from types import SimpleNamespace

import pytest

from src.access.certs import TrustedCAs
from src.accounts import ldap
from tests.unit.access.test_certs import make_cert, pem


def test_the_context_requires_the_certificate_and_the_name():
	"""Without trusted CAs: certificate required, host name checked."""
	context = ldap.VerifiedTls().context()
	assert context.verify_mode == ssl.CERT_REQUIRED
	assert context.check_hostname is True


def test_the_trusted_cas_come_on_top_of_the_systems():
	"""The organisation's CA is added: the context holds one more CA
	certificate than the operating system's alone."""
	ca, _ = make_cert(("Corp Root CA",), ca=True)
	alone = ldap.VerifiedTls().context().cert_store_stats()["x509_ca"]
	added = ldap.VerifiedTls(pem(ca).decode()).context().cert_store_stats()["x509_ca"]
	assert added == alone + 1


def test_every_server_gets_the_verifying_tls_with_the_trusted_cas():
	"""make_server gives ldap3 a VerifiedTls holding the certs folder's
	trusted CA certificates, read at that moment."""
	ca, _ = make_cert(("Corp Root CA",), ca=True)
	TrustedCAs().add(pem(ca))
	try:
		server = ldap.make_server(SimpleNamespace(host="dc1.corp.local", port=636, use_ssl=True))
		assert isinstance(server.tls, ldap.VerifiedTls)
		assert server.tls.cadata == pem(ca).decode()
	finally:
		TrustedCAs().path.unlink(missing_ok=True)


@pytest.mark.parametrize("message,words", [
	("socket ssl wrapping error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
	 "unable to get local issuer certificate (_ssl.c:1000)", "isn't trusted - add your"),
	("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate",
	 "isn't trusted - add your"),
	("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired",
	 "has expired - its administrator must renew it"),
	("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate is not yet valid",
	 "isn't valid yet"),
	("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, "
	 "certificate is not valid for 'dc1.corp.local'.", "doesn't name dc1.corp.local"),
])
def test_certificate_failures_in_words(message, words):
	"""Each kind of refused certificate gets its own words."""
	assert words in ldap.explain(Exception(message), "dc1.corp.local")


def test_other_errors_are_passed_through():
	"""Anything else is said as ldap3 says it."""
	assert ldap.explain(Exception("invalid credentials"), "dc1") == "invalid credentials"


def test_the_context_requires_tls_1_2_whatever_the_platform_allows(monkeypatch):
	"""Even where the platform's default context allows TLS 1.0 (an old
	OpenSSL configuration), LDAPS asks for TLS 1.2 or newer."""
	default = ssl.create_default_context

	def lax() -> ssl.SSLContext:
		context = default()
		context.minimum_version = ssl.TLSVersion.TLSv1
		return context
	monkeypatch.setattr(ldap.ssl, "create_default_context", lax)
	assert ldap.VerifiedTls().context().minimum_version == ssl.TLSVersion.TLSv1_2
