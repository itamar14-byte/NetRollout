"""LDAP helpers that need no directory: escaping, empty-password refusal,
timeouts, and unreachable-server classification."""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from ldap3.core.exceptions import LDAPException

import src.encryption as enc
from src.accounts import ldap


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
	assert ldap.constructed_dn(server, "jdoe,ou=admins") == \
	       r"uid=jdoe\,ou\=admins,dc=corp,dc=test"


def test_user_filter_escapes_metacharacters(server):
	"""Filter metacharacters in a username (`*`, `(`, `)`) are hex-escaped in the
	search filter."""
	assert ldap.user_filter(server, "*)(uid=*") == r"(uid=\2a\29\28uid=\2a)"


@pytest.mark.parametrize("username,password", [("jdoe", ""), ("", "pw")])
def test_empty_credentials_never_reach_the_network(server, username, password):
	"""An empty password or username returns None without opening a connection."""
	with patch.object(ldap, "_connection") as conn:
		assert ldap.authenticate(server, username, password) is None
	conn.assert_not_called()


def test_timeouts_are_configured(server):
	"""The server object gets CONNECT_TIMEOUT and the connection RECEIVE_TIMEOUT."""
	assert ldap.make_server(server).connect_timeout == ldap.CONNECT_TIMEOUT
	conn = ldap._connection(ldap.make_server(server))
	assert conn.receive_timeout == ldap.RECEIVE_TIMEOUT


def test_unreachable_directory_is_unavailable_not_bad_credentials(server):
	"""A refused connection raises LdapUnavailable from authenticate, not a
	plain refusal; the admin test tools report status error instead of raising."""
	with pytest.raises(ldap.LdapUnavailable):
		ldap.authenticate(server, "jdoe", "pw")  # port 1: refused
	# the admin test tools report it instead of raising
	assert ldap.test_user(server, "jdoe", "pw")["status"] == "error"
	assert ldap.test_connection(server)["status"] == "error"


def test_browsing_without_service_account_reports_error(server):
	"""Browsing the tree without a service account (bind type simple) reports
	status error."""
	server.bind_type = "simple"
	assert ldap.walk_tree(server)["status"] == "error"


# ── Branches a real directory can't easily produce ───────────────────────────

def test_closing_ignores_an_unbind_error():
	"""_close swallows an LDAPException from unbind (and does nothing for None)."""
	conn = MagicMock()
	conn.unbind.side_effect = LDAPException("connection already gone")
	ldap._close(conn)
	conn.unbind.assert_called_once_with()
	ldap._close(None)


def _root_dse(server, other, naming_contexts):
	"""fetch_base_dn against a fake directory whose root entry has `other`
	attributes and `naming_contexts`; :returns: its answer."""
	fake = SimpleNamespace(info=SimpleNamespace(other=other,
	                                            naming_contexts=naming_contexts))
	conn = MagicMock()
	with patch.object(ldap, "make_server", return_value=fake), \
			patch.object(ldap, "_connection", return_value=conn) as made:
		answer = ldap.fetch_base_dn(server)
	made.assert_called_once_with(fake)
	conn.open.assert_called_once_with()
	conn.unbind.assert_called_once_with()
	return answer


def test_base_dn_prefers_active_directorys_default_naming_context(server):
	"""With defaultNamingContext (AD) in the root entry, that is the base DN, not
	the first naming context."""
	assert _root_dse(server, {"defaultNamingContext": ["DC=corp,DC=example"]},
	                 ["CN=Configuration,DC=corp,DC=example"]) == \
	       {"status": "ok", "base_dn": "DC=corp,DC=example"}


def test_base_dn_falls_back_to_the_first_naming_context(server):
	"""Without defaultNamingContext the first naming context is the base DN."""
	assert _root_dse(server, {}, ["dc=corp,dc=test", "dc=other"]) == \
	       {"status": "ok", "base_dn": "dc=corp,dc=test"}


@pytest.mark.parametrize("naming_contexts", [[], None])
def test_base_dn_without_any_naming_context_is_an_error(server, naming_contexts):
	"""No defaultNamingContext and no naming contexts: status error "Could not
	determine base DN"."""
	assert _root_dse(server, {}, naming_contexts) == \
	       {"status": "error", "message": "Could not determine base DN"}


def test_base_dn_of_an_unreachable_directory_is_an_error(server):
	"""A directory nobody listens on: status error with ldap3's message."""
	answer = ldap.fetch_base_dn(server)
	assert answer["status"] == "error" and answer["message"]


@pytest.mark.parametrize("bind_type", ["anonymous", "", None])
def test_an_unknown_bind_type_is_refused_without_connecting(server, bind_type):
	"""test_connection says "Unknown bind type" and test_user "Invalid bind type"
	for a bind type other than regular / simple, without opening a connection."""
	server.bind_type = bind_type
	with patch.object(ldap, "_connection") as conn:
		assert ldap.test_connection(server) == {"status": "error",
		                                        "message": "Unknown bind type"}
		assert ldap.test_user(server, "jdoe", "pw") == {"status": "error",
		                                                "message": "Invalid bind type"}
	conn.assert_not_called()


def test_group_membership_needs_a_service_account(server):
	"""Without a service account (bind type simple) no group is looked at: None,
	no connection opened."""
	server.bind_type = "simple"
	with patch.object(ldap, "_connection") as conn:
		assert ldap.check_group_membership(server, "jdoe", "pw", []) is None
	conn.assert_not_called()


def test_a_failing_membership_search_makes_the_directory_unavailable(server):
	"""The user authenticated, then the group search fails (the connection lost):
	check_group_membership raises LdapUnavailable with ldap3's message and the
	service connection is closed."""
	conn = MagicMock()
	conn.search.side_effect = LDAPException("connection lost")
	groups = [SimpleNamespace(group_dn="cn=netops,dc=corp,dc=test", role="operator")]
	with patch.object(ldap, "authenticate", return_value="uid=jdoe,dc=corp,dc=test"), \
			patch.object(ldap, "service_bind", return_value=conn):
		with pytest.raises(ldap.LdapUnavailable, match="connection lost"):
			ldap.check_group_membership(server, "jdoe", "pw", groups)
	conn.unbind.assert_called_once_with()


def test_user_details_need_a_service_account(server):
	"""Without a service account the details aren't read: None, no connection."""
	server.bind_type = "simple"
	with patch.object(ldap, "_connection") as conn:
		assert ldap.fetch_user_details(server, "jdoe") is None
	conn.assert_not_called()


def test_user_details_of_an_unreachable_directory_are_none(server):
	"""The directory failing (port 1: refused) gives None, not an exception."""
	assert ldap.fetch_user_details(server, "jdoe") is None


def test_browsing_an_unreachable_directory_is_an_error(server):
	"""walk_tree with the service bind failing: status error with ldap3's message."""
	answer = ldap.walk_tree(server)
	assert answer["status"] == "error" and answer["message"]


def _entry(dn, classes, **attributes):
	"""A search result entry as ldap3 gives it: each attribute with its values
	(an empty list: requested, but the entry holds none)."""
	return SimpleNamespace(entry_dn=dn, objectClass=classes,
	                       **{k: SimpleNamespace(values=v) for k, v in attributes.items()})


def test_tree_labels_use_the_first_value_else_the_rdn(server):
	"""Labels are an attribute's first value - one of several cn values, not
	the list - and, for an entry without that attribute, its RDN's value;
	never an empty attribute shown as "[]". A user without the cn_identifier
	attribute has its label as username."""
	conn = MagicMock()
	conn.entries = [
		_entry("ou=Lab,dc=corp,dc=test", ["organizationalUnit"], ou=[], cn=[]),
		_entry("cn=ops,dc=corp,dc=test", ["groupOfNames"], cn=["ops", "operations"]),
		_entry("cn=Kim Lee,dc=corp,dc=test", ["inetOrgPerson"], cn=[], uid=[]),
	]
	with patch.object(ldap, "service_bind", return_value=conn):
		answer = ldap.walk_tree(server)
	assert answer == {"status": "ok", "entries": [
		{"type": "ou", "dn": "ou=Lab,dc=corp,dc=test", "label": "Lab", "username": None},
		{"type": "group", "dn": "cn=ops,dc=corp,dc=test", "label": "ops",
		 "username": None},
		{"type": "user", "dn": "cn=Kim Lee,dc=corp,dc=test", "label": "Kim Lee",
		 "username": "Kim Lee"},
	]}
