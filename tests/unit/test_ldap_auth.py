"""LDAP helpers that need no directory: escaping, empty-password refusal,
timeouts, and unreachable-server classification."""
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
from src import ldap_auth


@pytest.fixture
def server():
	"""An LDAP server config with a service account on 127.0.0.1 port 1, where
	nothing listens (encryption initialised with a fresh key)."""
	os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
	enc.init_encryption(None)
	return SimpleNamespace(host="127.0.0.1", port=1, use_ssl=False,
	                       base_dn="dc=corp,dc=test", cn_identifier="uid",
	                       bind_type="regular", bind_dn="cn=svc,dc=corp,dc=test",
	                       bind_password=enc.encrypt("svc"))


def test_constructed_dn_escapes_the_username(server):
	"""A username with DN syntax (`,` and `=`) is escaped in the constructed DN."""
	assert ldap_auth.constructed_dn(server, "jdoe,ou=admins") == \
	       r"uid=jdoe\,ou\=admins,dc=corp,dc=test"


def test_user_filter_escapes_metacharacters(server):
	"""Filter metacharacters in a username (`*`, `(`, `)`) are hex-escaped in the
	search filter."""
	assert ldap_auth.user_filter(server, "*)(uid=*") == r"(uid=\2a\29\28uid=\2a)"


@pytest.mark.parametrize("username,password", [("jdoe", ""), ("", "pw")])
def test_empty_credentials_never_reach_the_network(server, username, password):
	"""An empty password or username returns None without opening a connection."""
	with patch.object(ldap_auth, "_connection") as conn:
		assert ldap_auth.authenticate(server, username, password) is None
	conn.assert_not_called()


def test_timeouts_are_configured(server):
	"""The server object gets CONNECT_TIMEOUT and the connection RECEIVE_TIMEOUT."""
	assert ldap_auth.make_server(server).connect_timeout == ldap_auth.CONNECT_TIMEOUT
	conn = ldap_auth._connection(ldap_auth.make_server(server))
	assert conn.receive_timeout == ldap_auth.RECEIVE_TIMEOUT


def test_unreachable_directory_is_unavailable_not_bad_credentials(server):
	"""A refused connection raises LdapUnavailable from authenticate, not a
	plain refusal; the admin test tools report status error instead of raising."""
	with pytest.raises(ldap_auth.LdapUnavailable):
		ldap_auth.authenticate(server, "jdoe", "pw")  # port 1: refused
	# the admin test tools report it instead of raising
	assert ldap_auth.test_user(server, "jdoe", "pw")["status"] == "error"
	assert ldap_auth.test_connection(server)["status"] == "error"


def test_browsing_without_service_account_reports_error(server):
	"""Browsing the tree without a service account (bind type simple) reports
	status error."""
	server.bind_type = "simple"
	assert ldap_auth.walk_tree(server)["status"] == "error"
