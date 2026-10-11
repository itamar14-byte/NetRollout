"""LDAPS against the real (ephemeral) OpenLDAP directory, which serves a
certificate for "localhost" issued by a test CA: always verified - trusted
once the CA is among the trusted CA certificates, refused in words while it
isn't, and refused for a name the certificate doesn't hold."""
import os
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
from src.access.certs import TrustedCAs
from src.accounts import ldap
from src.accounts.ldap import LdapUnavailable
from tests.integration.conftest import (LDAP_BASE, LDAP_SERVICE_DN, LDAP_SERVICE_PW, LDAP_TLS,
                                        LDAP_USERS)

pytestmark = pytest.mark.ldap

JDOE_DN, JDOE_PW = LDAP_USERS["jdoe"]
UNTRUSTED = ("The directory server's certificate isn't trusted - add your organisation's CA "
             "certificate under Server Management -> TLS Certificate -> Trusted CA certificates.")


@pytest.fixture
def ldaps(ldap_directory):
	"""An LDAPServer-shaped object for the directory's LDAPS (by the name its
	certificate holds), with an encryption key for the service account's
	password; no trusted CA certificates before or after."""
	os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
	enc.init_encryption(None)
	trusted = TrustedCAs()
	trusted.path.unlink(missing_ok=True)
	yield SimpleNamespace(
		host="localhost", port=LDAP_TLS["port"], use_ssl=True, base_dn=LDAP_BASE,
		cn_identifier="uid", bind_type="regular", bind_dn=LDAP_SERVICE_DN,
		bind_password=enc.encrypt(LDAP_SERVICE_PW))
	trusted.path.unlink(missing_ok=True)


def test_an_untrusted_certificate_is_refused_in_words(ldaps):
	"""Without the test CA trusted, the Test button and a sign-in are refused:
	the certificate isn't trusted, and where to add the CA."""
	assert ldap.Directory(ldaps).test_connection() == {"status": "error", "message": UNTRUSTED}
	with pytest.raises(LdapUnavailable, match="isn't trusted"):
		ldap.Directory(ldaps).authenticate("jdoe", JDOE_PW)


def test_a_trusted_ca_lets_ldaps_through(ldaps):
	"""With the test CA added to the trusted CA certificates, the same server
	verifies: the Test button connects and jdoe signs in."""
	TrustedCAs().add(LDAP_TLS["ca"].encode())
	assert ldap.Directory(ldaps).test_connection() == {"status": "ok",
	                                                   "message": "Connection established"}
	assert ldap.Directory(ldaps).authenticate("jdoe", JDOE_PW) == JDOE_DN


def test_a_name_the_certificate_doesnt_hold_is_refused(ldaps):
	"""Trusted, but reached as 127.0.0.1 (the certificate names localhost
	only): refused, saying the certificate doesn't name it."""
	TrustedCAs().add(LDAP_TLS["ca"].encode())
	by_ip = SimpleNamespace(**{**vars(ldaps), "host": "127.0.0.1"})
	answer = ldap.Directory(by_ip).test_connection()
	assert answer == {"status": "error", "message": (
		"The directory server's certificate doesn't name 127.0.0.1 - connect to it by a "
		"name its certificate holds, or ask for a certificate that names 127.0.0.1.")}
