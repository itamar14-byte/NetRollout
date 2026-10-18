"""Server Management -> LDAP: the directory servers (add, edit, test, explore,
import users) and their groups. URLs under /admin/server/ldap, as before."""
import uuid
from typing import Any

from flask import Blueprint, Request, Response, request, jsonify
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required

from src.accounts.ldap import Directory, LdapUnavailable
from src.accounts.users import (ROLES, AccountError, Accounts, directory_sets_role,
                                has_no_directory, is_reserved)
from src.audit import AuditAction
from src.db.tables import LDAPServer, LDAPGroup, BindType, User
from src.encryption import encrypt
from src.rollout import inputs
from src.webapp.app import current_app
from src.webapp.http import ok, err, require_admin, with_form


bp = Blueprint('admin_ldap', __name__, url_prefix='/admin/server')


LDAP_BIND_TYPES = (BindType.REGULAR, BindType.SIMPLE)   # with a service account / without


def unload_ldap_data(req: Request) -> dict[str, str]:
	""":returns: the LDAP server form's fields, stripped (a missing one "")"""
	label = req.form.get("label", "").strip()
	ip = req.form.get("ip", "").strip()
	port = req.form.get("port", "389").strip()
	base_dn = req.form.get("base_dn", "").strip()
	cn_identifier = req.form.get("cn_identifier", "").strip()
	bind_type = req.form.get("bind_type", "").strip()
	bind_dn = req.form.get("bind_dn", "").strip()
	use_ssl = req.form.get("use_ssl", "").strip()
	is_active = req.form.get("is_active", "").strip()
	bind_password = req.form.get("bind_password", "").strip()

	return {"label": label,
	        "ip": ip,
	        "port": port,
	        "base_dn": base_dn,
	        "cn_identifier": cn_identifier,
	        "bind_type": bind_type,
	        "bind_dn": bind_dn,
	        "use_ssl": use_ssl,
	        "is_active": is_active,
	        "bind_password": bind_password
	        }


# A server's settings the audit log records (the bind password only as
# "set" / "changed", never its value): where users' passwords go at sign-in
AUDITED_SETTINGS = ("name", "host", "port", "use_ssl", "base_dn", "cn_identifier",
                    "bind_type", "bind_dn", "is_active")


def server_settings(server: LDAPServer) -> dict[str, Any]:
	""":returns: the server's AUDITED_SETTINGS, by name"""
	return {name: getattr(server, name) for name in AUDITED_SETTINGS}


def settings_changed(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
	""":returns: each setting that differs, {name: {"from": old, "to": new}}"""
	return {name: {"from": before[name], "to": after[name]}
	        for name in AUDITED_SETTINGS if before[name] != after[name]}


def ldap_problem(server_input: dict[str, str], new: bool) -> str | None:
	"""The LDAP server form's check (the page's can be bypassed).

	:param new: a new server needs a bind type; a save may leave it blank
	 (the stored one stays)
	:returns: what's wrong, in words; None when valid"""
	if not inputs.validate_port(server_input["port"]):
		return "The port is a number from 1 to 65535."
	if (new or server_input["bind_type"]) and \
			server_input["bind_type"] not in LDAP_BIND_TYPES:
		return "The bind type is regular or simple."
	return None


@bp.route("/ldap", methods=["GET"])
@login_required
@require_admin
def admin_server_ldap_get() -> Response:
	""":returns: the LDAP servers, every column but the bind password"""
	with current_app.backend.postgres.get_session() as db_session:
		servers = db_session.query(LDAPServer).all()
		result = []

		for s in servers:
			servers_dict = {c.name: getattr(s, c.name) for
			                c in s.__table__.columns}
			del servers_dict["bind_password"]
			servers_dict["id"] = str(servers_dict["id"])
			result.append(servers_dict)
	return jsonify(result)


@bp.route("/ldap/new", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_new() -> ResponseReturnValue:
	"""Add an LDAP server from the form (the bind password encrypted)."""
	server_input = unload_ldap_data(request)
	if problem := ldap_problem(server_input, new=True):
		return err(problem, 422)

	with current_app.backend.postgres.get_session() as db_session:
		row = LDAPServer(
			name=server_input["label"],
			host=server_input["ip"],
			port=int(server_input["port"]),
			base_dn=server_input["base_dn"],
			cn_identifier=server_input["cn_identifier"],
			bind_type=server_input["bind_type"],
			bind_dn=server_input["bind_dn"],
			use_ssl=server_input["use_ssl"].lower() == "true",
			is_active=server_input["is_active"].lower() == "true",
			bind_password=encrypt(server_input["bind_password"])
			if server_input["bind_password"] else None)

		db_session.add(row)
		db_session.flush()       # its id, for the audit row
		created: dict[str, Any] = server_settings(row)
		if row.bind_password:
			created["bind_password"] = "set"
		current_app.web.audit(AuditAction.ADMIN_LDAP_SERVER_SAVE,
		                      object_type="LDAPServer", object_id=row.id,
		                      object_label=row.name, success=True,
		                      detail={"created": created})
	return ok()


@bp.route("/ldap/<uuid:server_id>/save", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_save(server_id: uuid.UUID) -> ResponseReturnValue:
	"""Change an LDAP server: a blank field keeps the stored value (the bind
	password too); the port, SSL and active are always taken from the form.
	Audited with what changed, from and to (the bind password only as
	"changed")."""
	server_input = unload_ldap_data(request)
	if problem := ldap_problem(server_input, new=False):
		return err(problem, 422)

	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		before = server_settings(srv)
		srv.name = server_input["label"] if server_input["label"] else srv.name
		srv.host = server_input["ip"] if server_input["ip"] else srv.host
		srv.port = int(server_input["port"])
		srv.base_dn = server_input["base_dn"] if server_input[
			"base_dn"] else srv.base_dn
		srv.cn_identifier = server_input["cn_identifier"] if \
			server_input["cn_identifier"] else srv.cn_identifier
		srv.bind_type = server_input["bind_type"] if \
			server_input["bind_type"] else srv.bind_type
		srv.bind_dn = server_input["bind_dn"] if server_input[
			"bind_dn"] else srv.bind_dn
		srv.use_ssl = server_input["use_ssl"].lower() == "true"
		srv.is_active = server_input["is_active"].lower() == "true"
		srv.bind_password = encrypt(server_input["bind_password"]) if \
			server_input["bind_password"] else srv.bind_password
		detail: dict[str, Any] = {"changes": settings_changed(before, server_settings(srv))}
		if server_input["bind_password"]:
			detail["bind_password"] = "changed"
		current_app.web.audit(AuditAction.ADMIN_LDAP_SERVER_SAVE,
		                      object_type="LDAPServer", object_id=srv.id,
		                      object_label=srv.name, success=True, detail=detail)
	return ok()


@bp.route("/ldap/<uuid:server_id>/users")
@login_required
@require_admin
def admin_server_ldap_users(server_id: uuid.UUID) -> ResponseReturnValue:
	"""The accounts that sign in through a server - what deleting it asks
	about: JSON users [{username, role, is_active}], by username."""
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.get(LDAPServer, server_id)
		if not srv:
			return err("Server not found", 404)
		users = [{"username": u.username, "role": u.role, "is_active": u.is_active}
		         for u in sorted(srv.users, key=lambda u: u.username)]
	return ok(users=users)


@bp.route("/ldap/<uuid:server_id>/delete", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_delete(server_id: uuid.UUID) -> ResponseReturnValue:
	"""Delete a server: JSON {delete_users} - true deletes its users with
	only their own things (global devices stay), else (or no body) they're
	kept but disabled. Refused when it would leave no administrator who can sign in,
	or the admin deleting it signs in through it. Audited with the users
	deleted or disabled."""
	data = request.get_json(silent=True)
	delete_users = isinstance(data, dict) and data.get("delete_users") is True
	try:
		with current_app.backend.postgres.get_session() as db_session:
			srv = db_session.get(LDAPServer, server_id)
			if not srv:
				return err("Server not found", 404)
			if any(u.id == current_user.id for u in srv.users):
				return err("You sign in through this server. Sign in with another "
				           "administrator account to delete it.", 409)
			name = srv.name
			names = Accounts(db_session).delete_directory(srv, delete_users)
	except AccountError as e:
		return err(str(e), 409)
	current_app.web.audit(AuditAction.ADMIN_LDAP_SERVER_DELETE,
	                      object_type="LDAPServer", object_id=server_id,
	                      object_label=name, success=True,
	                      detail={"users_deleted" if delete_users else "users_disabled": names})
	return ok(users=names, deleted=delete_users)


@bp.route("/ldap/<uuid:server_id>/test", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_test(server_id: uuid.UUID) -> ResponseReturnValue:
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(Directory(srv).test_connection())


@bp.route("/ldap/<uuid:server_id>/test_user", methods=["POST"])
@login_required
@require_admin
@with_form("username", "password")
def admin_server_ldap_test_user(server_id: uuid.UUID, data: Any) -> ResponseReturnValue:
	username = data.get("username", "").strip()
	password = data.get("password", "").strip()
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(Directory(srv).test_user(username, password))


@bp.route("/ldap/<uuid:server_id>/fetch_dn", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_fetch_dn(server_id: uuid.UUID) -> ResponseReturnValue:
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(Directory(srv).fetch_base_dn())


@bp.route("/ldap/<uuid:server_id>/explore", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_explore(server_id: uuid.UUID) -> ResponseReturnValue:
	dn = request.form.get("dn", "").strip() or None
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(Directory(srv).walk_tree(dn))


@bp.route("/ldap/<uuid:server_id>/import", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_import(server_id: uuid.UUID) -> ResponseReturnValue:
	"""Users and groups picked in the directory browser: users become LDAP
	accounts (approved, active), groups become mappable groups (operator);
	ones that exist are skipped, and so are malformed items. JSON [{type:
	user | group, username | dn, label}], or {items: [...], reconnect: true}:
	then a name whose account lost its directory (has_no_directory) is
	reconnected to this one instead of skipped (Accounts.reconnect: it must
	exist here, takes its email and name, stays disabled). Where the
	directory sets roles (users.directory_sets_role) a new or reconnected
	user gets the role their group rules give, as at sign-in (the groups
	imported here included); else a new one is an operator.

	:returns: {users_created, groups_created, reconnected, skipped, admins};
	 422 when the body is neither, or a name to reconnect isn't in the
	 directory; 502 when the directory can't be asked (nothing imported)"""
	body = request.get_json(silent=True)
	if body is None:
		body = []
	reconnect = isinstance(body, dict) and body.get("reconnect") is True
	items = body.get("items") if isinstance(body, dict) else body
	if not isinstance(items, list):
		return err("The import is a list of users and groups.", 422)
	users_created = 0
	groups_created = 0
	skipped = 0
	created: list[User] = []
	to_reconnect: list[User] = []

	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		accounts = Accounts(db_session)

		for item in items:
			if not isinstance(item, dict) or not (
					(item.get("type") == "user" and item.get("username"))
					or (item.get("type") == "group" and item.get("dn"))):
				skipped += 1
				continue
			if item["type"] == "user":
				# usernames are compared as sign-in compares them: whatever the case;
				# the reserved name is never an account
				existing = accounts.by_name_ci(item["username"])
				if (reconnect and len(existing) == 1 and has_no_directory(existing[0])
						and existing[0] not in to_reconnect):
					to_reconnect.append(existing[0])
					continue
				if existing or is_reserved(item["username"]):
					skipped += 1
					continue
				created.append(accounts.new_ldap(item["username"], server_id))
				users_created += 1

			elif item["type"] == "group":
				if db_session.query(LDAPGroup).filter_by(
						group_dn=item["dn"], ldap_server_id=server_id).first():
					skipped += 1
					continue
				db_session.add(LDAPGroup(
					group_dn=item["dn"],
					label=item.get("label") or item["dn"],
					ldap_server_id=server_id
				))
				groups_created += 1

		db_session.flush()
		directory = Directory(srv)
		try:
			reconnected = (accounts.reconnect(to_reconnect, srv, directory.find_user)
			               if to_reconnect else [])
			admins = directory.set_roles(created + to_reconnect)
		except AccountError as e:
			db_session.rollback()
			return err(f"{e} Nothing was imported.", 422)
		except LdapUnavailable as e:
			db_session.rollback()
			return err(f"The directory couldn't be asked about the users - "
			           f"nothing was imported: {e}", 502)
		current_app.web.audit(AuditAction.ADMIN_LDAP_IMPORT, success=True,
		                      detail={"users": users_created,
		                              "groups": groups_created,
		                              "reconnected": reconnected,
		                              "skipped": skipped, "admins": admins})

	return ok(users_created=users_created, groups_created=groups_created,
	          reconnected=len(reconnected), skipped=skipped, admins=admins)


@bp.route("/ldap/<uuid:server_id>/groups", methods=["GET"])
@login_required
@require_admin
def admin_server_ldap_groups(server_id: uuid.UUID) -> Response:
	with current_app.backend.postgres.get_session() as db_session:
		groups = db_session.query(LDAPGroup).filter_by(
			ldap_server_id=server_id).all()
		result = [{"id": str(g.id), "group_dn": g.group_dn, "label": g.label,
		           "role": g.role, "is_active": g.is_active} for g in groups]
	return jsonify(result)


@bp.route("/ldap/<uuid:server_id>/groups/<uuid:group_id>/toggle",
          methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_group_toggle(server_id: uuid.UUID,
                                   group_id: uuid.UUID) -> ResponseReturnValue:
	with current_app.backend.postgres.get_session() as db_session:
		g = db_session.query(LDAPGroup).filter_by(
			id=group_id, ldap_server_id=server_id).first()
		if not g:
			return err("Group not found", 404)
		new_state = not g.is_active
		g.is_active = new_state
		current_app.web.audit(AuditAction.ADMIN_LDAP_GROUP_TOGGLE,
		                      object_type="LDAPGroup",
		                      object_label=g.label, success=True,
		                      detail={"is_active": g.is_active})
	return ok(is_active=new_state)


@bp.route("/ldap/<uuid:server_id>/groups/<uuid:group_id>/role",
          methods=["POST"])
@login_required
@require_admin
@with_form("role")
def admin_server_ldap_group_role(server_id: uuid.UUID, group_id: uuid.UUID,
                                 data: Any) -> ResponseReturnValue:
	"""The role a group rule gives its members: operator or admin. Directory
	users' roles follow at their next sign-in (users.directory_sets_role)."""
	role = data.get("role", "")
	if role not in ROLES:
		return err("The role is operator or admin.", 422)
	with current_app.backend.postgres.get_session() as db_session:
		g = db_session.query(LDAPGroup).filter_by(
			id=group_id, ldap_server_id=server_id).first()
		if not g:
			return err("Group not found", 404)
		before, g.role = g.role, role
		current_app.web.audit(AuditAction.ADMIN_LDAP_GROUP_ROLE,
		                      object_type="LDAPGroup", object_id=g.id,
		                      object_label=g.label, success=True,
		                      detail={"from": before, "to": role})
	return ok(role=role)


@bp.route("/ldap/<uuid:server_id>/groups/<uuid:group_id>/delete",
          methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_group_delete(server_id: uuid.UUID,
                                   group_id: uuid.UUID) -> ResponseReturnValue:
	with current_app.backend.postgres.get_session() as db_session:
		g = db_session.query(LDAPGroup).filter_by(
			id=group_id, ldap_server_id=server_id).first()
		if not g:
			return err("Group not found", 404)
		db_session.delete(g)
		current_app.web.audit(AuditAction.ADMIN_LDAP_GROUP_DELETE,
		                      object_type="LDAPGroup",
		                      object_label=g.label, success=True)
	return ok()
