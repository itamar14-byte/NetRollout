from ldap3 import Server, Connection, ALL, SIMPLE, SUBTREE, LEVEL
from ldap3.core.exceptions import LDAPSocketOpenError, LDAPBindError

from db.tables import LDAPServer, LDAPGroup
from encryption import decrypt


def user_dn(server: LDAPServer, username: str) -> str:
	return f"{server.cn_identifier}={username},{server.base_dn}"


def make_server(server: LDAPServer) -> Server:
	return Server(host=server.host, port=server.port, use_ssl=server.use_ssl,
	              get_info=ALL)


def service_bind(server: LDAPServer) -> Connection | None:
	ldap_server = make_server(server)
	if server.bind_type == "regular":
		conn = Connection(ldap_server, user=server.bind_dn,
		                  password=decrypt(server.bind_password),
		                  authentication=SIMPLE,
		                  raise_exceptions=True)
	else:
		return
	conn.bind()
	return conn


def user_bind(server: LDAPServer, username: str, password: str) -> bool:
	dn = user_dn(server, username)
	ldap_server = make_server(server)
	try:
		conn = Connection(ldap_server, user=dn, password=password,
		                  authentication=SIMPLE, raise_exceptions=True)
		conn.bind()
		return True
	except LDAPBindError:
		return False


def test_connection(server: LDAPServer) -> dict[str, str]:
	try:
		if server.bind_type not in ("regular", "simple"):
			return {"status": "error", "message": "Unknown bind type"}
		elif server.bind_type == "regular":
			service_bind(server)
		elif server.bind_type == "simple":
			conn = Connection(make_server(server), raise_exceptions=True)
			conn.open()
		return {"status": "ok", "message": "Connection established"}

	except (LDAPBindError, LDAPSocketOpenError) as e:
		return {"status": "error", "message": str(e)}


def test_user(server, username, password) -> dict[str, bool | str]:
	try:
		if server.bind_type not in ("regular", "simple"):
			return {"status": "error", "message": "Invalid bind type"}
		result = user_bind(server, username, password)
		if result:
			return {"status": "ok", "message": f"User {username} connected"}
		else:
			return {"status": "error", "message": f"User {username} failed"}
	except (LDAPBindError, LDAPSocketOpenError) as e:
		return {"status": "error", "message": str(e)}


def check_group_membership(server: LDAPServer, username: str, password: str,
                           groups: list[LDAPGroup]) -> tuple | None:
	if server.bind_type != "regular":
		return None
	try:
		user = user_bind(server, username, password)
		if not user:
			return None
		srv_conn = service_bind(server)
		for g in groups:
			srv_conn.search(
				search_base=g.group_dn,
				search_filter=f"(member={user_dn(server, username)})",
				search_scope=SUBTREE
			)
			if srv_conn.entries:
				return g.group_dn, g.role
		return None
	except (LDAPBindError, LDAPSocketOpenError):
		return None


def fetch_user_details(server: LDAPServer, username: str) -> dict | None:
	try:
		srv_conn = service_bind(server)
		srv_conn.search(
			search_base=server.base_dn,
			search_filter=f"({server.cn_identifier}={username})",
			search_scope=SUBTREE,
			attributes=['mail', 'displayName', 'cn']
		)
		if not srv_conn.entries:
			return None
		entry = srv_conn.entries[0]
		email = str(entry.mail) if entry.mail else None
		full_name = str(entry.displayName) if entry.displayName else str(
			entry.cn) if entry.cn else username
		return {"email": email, "full_name": full_name}
	except (LDAPBindError, LDAPSocketOpenError):
		return None


def fetch_base_dn(server: LDAPServer) -> dict[str, str]:
	try:
		ldap_server = make_server(server)
		conn = Connection(ldap_server, raise_exceptions=True)
		conn.open()
		info = ldap_server.info
		dn = (info.other.get("defaultNamingContext", [None])[0]
		      or (info.naming_contexts[0] if info.naming_contexts else None))
		if dn:
			return {"status": "ok", "base_dn": dn}
		return {"status": "error", "message": "Could not determine base DN"}
	except (LDAPBindError, LDAPSocketOpenError) as e:
		return {"status": "error", "message": str(e)}


def walk_tree(server: LDAPServer, dn: str = None) -> list[dict[str,
str | None]] | dict[str, str]:
	try:
		scope = dn or server.base_dn
		srv_conn = service_bind(server)
		srv_conn.search(
			search_base=scope,
			search_filter='(objectClass=*)',
			search_scope=LEVEL,
			attributes=['objectClass', 'cn', server.cn_identifier]
		)

		results = []

		for entry in srv_conn.entries:
			classes = [str(c).lower() for c in entry.objectClass]
			if "organizationalunit" in classes:
				results.append({"type": "ou", "dn": entry.entry_dn,
				                "label": str(entry.cn), "username": None})

			elif "group" in classes:
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
	except (LDAPBindError, LDAPSocketOpenError) as e:
		return {"status": "error", "message": str(e)}
