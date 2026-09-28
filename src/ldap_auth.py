from ldap3 import Server, Connection, ALL, SIMPLE, SUBTREE, LEVEL, BASE
from ldap3.core.exceptions import (LDAPException, LDAPBindError,
                                   LDAPInvalidCredentialsResult)
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from src.db.tables import LDAPServer, LDAPGroup
from src.encryption import decrypt

# Fail fast when the directory is unreachable instead of hanging the login
# request for the OS connect timeout
CONNECT_TIMEOUT = 5
RECEIVE_TIMEOUT = 10

# A rejected bind — wrong password or unknown DN. Anything else raised by
# ldap3 means the directory couldn't be used (down, timeout, bad config).
_BAD_CREDENTIALS = (LDAPBindError, LDAPInvalidCredentialsResult)


class LdapUnavailable(Exception):
	"""The directory couldn't be reached or used — distinct from a user
	entering wrong credentials, so login can say which one happened."""
	pass


def make_server(server: LDAPServer) -> Server:
	return Server(host=server.host, port=server.port, use_ssl=server.use_ssl,
	              get_info=ALL, connect_timeout=CONNECT_TIMEOUT)


def _connection(ldap_server: Server, **kwargs) -> Connection:
	return Connection(ldap_server, receive_timeout=RECEIVE_TIMEOUT,
	                  raise_exceptions=True, **kwargs)


def _close(conn: Connection | None) -> None:
	if conn is not None:
		try:
			conn.unbind()
		except LDAPException:
			pass


def service_bind(server: LDAPServer) -> Connection | None:
	"""Bound service-account connection, or None when the server has no
	service account. Raises LDAPException on failure; callers close it."""
	if server.bind_type != "regular":
		return None
	conn = _connection(make_server(server), user=server.bind_dn,
	                   password=decrypt(server.bind_password),
	                   authentication=SIMPLE)
	conn.bind()
	return conn


def constructed_dn(server: LDAPServer, username: str) -> str:
	# Only valid for flat directories (users directly under base_dn, named by
	# cn_identifier). Escaped so the username can't add RDNs to the DN.
	return f"{server.cn_identifier}={escape_rdn(username)},{server.base_dn}"


def user_filter(server: LDAPServer, username: str) -> str:
	return f"({server.cn_identifier}={escape_filter_chars(username)})"


def find_user_dn(server: LDAPServer, username: str) -> str | None:
	"""Search-then-bind: resolve the user's real DN with the service account
	(users can live anywhere under base_dn, e.g. nested AD OUs). Without a
	service account, fall back to the constructed DN. None if the user
	doesn't exist or the name is ambiguous. Raises LDAPException."""
	conn = service_bind(server)
	if conn is None:
		return constructed_dn(server, username)
	try:
		conn.search(search_base=server.base_dn,
		            search_filter=user_filter(server, username),
		            search_scope=SUBTREE, attributes=[])
		if len(conn.entries) != 1:
			return None
		return conn.entries[0].entry_dn
	finally:
		_close(conn)


def authenticate(server: LDAPServer, username: str, password: str) -> str | None:
	"""The user's DN if username/password are valid, None if they aren't.
	:raises LdapUnavailable: the directory couldn't be reached or used"""
	# Some servers treat a DN with an empty password as a *successful*
	# unauthenticated bind — never send one
	if not username or not password:
		return None
	try:
		dn = find_user_dn(server, username)
		if dn is None:
			return None
		conn = _connection(make_server(server), user=dn, password=password,
		                   authentication=SIMPLE)
		try:
			conn.bind()
			return dn
		except _BAD_CREDENTIALS:
			return None
		finally:
			_close(conn)
	except LDAPException as e:
		raise LdapUnavailable(str(e)) from e


def user_bind(server: LDAPServer, username: str, password: str) -> bool:
	""":raises LdapUnavailable: the directory couldn't be reached or used"""
	return authenticate(server, username, password) is not None


def test_connection(server: LDAPServer) -> dict[str, str]:
	conn = None
	try:
		if server.bind_type not in ("regular", "simple"):
			return {"status": "error", "message": "Unknown bind type"}
		elif server.bind_type == "regular":
			conn = service_bind(server)
		elif server.bind_type == "simple":
			conn = _connection(make_server(server))
			conn.open()
		return {"status": "ok", "message": "Connection established"}
	except LDAPException as e:
		return {"status": "error", "message": str(e)}
	finally:
		_close(conn)


def test_user(server, username, password) -> dict[str, bool | str]:
	if server.bind_type not in ("regular", "simple"):
		return {"status": "error", "message": "Invalid bind type"}
	try:
		if user_bind(server, username, password):
			return {"status": "ok", "message": f"User {username} connected"}
		return {"status": "error", "message": f"User {username} failed"}
	except LdapUnavailable as e:
		return {"status": "error", "message": str(e)}


def check_group_membership(server: LDAPServer, username: str, password: str,
                           groups: list[LDAPGroup]) -> tuple | None:
	"""(group_dn, role) of the first mapped group the authenticated user is a
	direct member of, else None.
	:raises LdapUnavailable: the directory couldn't be reached or used"""
	if server.bind_type != "regular":
		return None
	dn = authenticate(server, username, password)
	if dn is None:
		return None
	conn = None
	try:
		conn = service_bind(server)
		for g in groups:
			# The member value is a DN — it may contain filter metacharacters
			# (e.g. "cn=Smith\, Bob"), so it's escaped like any other value
			conn.search(search_base=g.group_dn,
			            search_filter=f"(member={escape_filter_chars(dn)})",
			            search_scope=BASE, attributes=[])
			if conn.entries:
				return g.group_dn, g.role
		return None
	except LDAPException as e:
		raise LdapUnavailable(str(e)) from e
	finally:
		_close(conn)


def fetch_user_details(server: LDAPServer, username: str) -> dict | None:
	# Display attributes only — failure is tolerated by the caller
	conn = None
	try:
		conn = service_bind(server)
		if conn is None:
			return None
		conn.search(search_base=server.base_dn,
		            search_filter=user_filter(server, username),
		            search_scope=SUBTREE,
		            attributes=['mail', 'displayName', 'cn'])
		if len(conn.entries) != 1:
			return None
		entry = conn.entries[0]
		email = str(entry.mail) if entry.mail else None
		full_name = str(entry.displayName) if entry.displayName else str(
			entry.cn) if entry.cn else username
		return {"email": email, "full_name": full_name}
	except LDAPException:
		return None
	finally:
		_close(conn)


def fetch_base_dn(server: LDAPServer) -> dict[str, str]:
	conn = None
	try:
		ldap_server = make_server(server)
		conn = _connection(ldap_server)
		conn.open()
		info = ldap_server.info
		dn = (info.other.get("defaultNamingContext", [None])[0]
		      or (info.naming_contexts[0] if info.naming_contexts else None))
		if dn:
			return {"status": "ok", "base_dn": dn}
		return {"status": "error", "message": "Could not determine base DN"}
	except LDAPException as e:
		return {"status": "error", "message": str(e)}
	finally:
		_close(conn)


def walk_tree(server: LDAPServer, dn: str = None) -> list[dict[str,
str | None]] | dict[str, str]:
	conn = None
	try:
		scope = dn or server.base_dn
		conn = service_bind(server)
		if conn is None:
			return {"status": "error",
			        "message": "Browsing requires a service account"}
		conn.search(
			search_base=scope,
			search_filter='(objectClass=*)',
			search_scope=LEVEL,
			attributes=['objectClass', 'cn', server.cn_identifier]
		)

		results = []

		for entry in conn.entries:
			classes = [str(c).lower() for c in entry.objectClass]
			if "organizationalunit" in classes:
				results.append({"type": "ou", "dn": entry.entry_dn,
				                "label": str(entry.cn), "username": None})

			# AD: group; OpenLDAP-style directories: groupOfNames / groupOfUniqueNames
			elif {"group", "groupofnames", "groupofuniquenames"} & set(classes):
				results.append(
					{"type": "group", "dn": entry.entry_dn,
					 "label": str(entry.cn),
					 "username": None})

			elif "person" in classes or "user" in classes:
				identifier = getattr(entry, server.cn_identifier, None)
				username = str(identifier) if identifier else str(entry.cn)
				results.append({"type": "user", "dn": entry.entry_dn,
				                "label": str(entry.cn), "username": username})
		return {"status": "ok", "entries": results}
	except LDAPException as e:
		return {"status": "error", "message": str(e)}
	finally:
		_close(conn)
