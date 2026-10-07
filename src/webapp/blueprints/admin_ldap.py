"""Server Management -> LDAP: the directory servers (add, edit, test, explore,
import users) and their groups. URLs under /admin/server/ldap, as before."""
import uuid
from typing import Any

from flask import Blueprint, Request, Response, request, jsonify
from flask.typing import ResponseReturnValue
from flask_login import login_required

from src.accounts.ldap import test_user, test_connection, fetch_base_dn, walk_tree
from src.db.tables import LDAPServer, LDAPGroup, User
from src.encryption import encrypt
from src.rollout import inputs as validation
from src.webapp.app import current_app
from src.webapp.http import ok, err, require_admin, with_form


bp = Blueprint('admin_ldap', __name__, url_prefix='/admin/server')


LDAP_BIND_TYPES = ("regular", "simple")   # with a service account / without


##############################Route Helpers#####################################
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


def ldap_problem(server_input: dict[str, str], new: bool) -> str | None:
	"""The LDAP server form's check (the page's can be bypassed).

	:param new: a new server needs a bind type; a save may leave it blank
	 (the stored one stays)
	:returns: what's wrong, in words; None when valid"""
	if not validation.validate_port(server_input["port"]):
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

		current_app.web.audit("admin.ldap_server_save",
		                      object_type="LDAPServer",
		                      object_label=server_input["label"], success=True)
	return ok()


@bp.route("/ldap/<uuid:server_id>/save", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_save(server_id: uuid.UUID) -> ResponseReturnValue:
	"""Change an LDAP server: a blank field keeps the stored value (the bind
	password too); the port, SSL and active are always taken from the form."""
	server_input = unload_ldap_data(request)
	if problem := ldap_problem(server_input, new=False):
		return err(problem, 422)

	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
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
		current_app.web.audit("admin.ldap_server_save",
		                      object_type="LDAPServer",
		                      object_label=srv.name, success=True)
	return ok()


@bp.route("/ldap/<uuid:server_id>/delete", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_delete(server_id: uuid.UUID) -> ResponseReturnValue:
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		db_session.delete(srv)
		current_app.web.audit("admin.ldap_server_delete",
		                      object_type="LDAPServer",
		                      object_label=srv.name, success=True)
	return ok()


@bp.route("/ldap/<uuid:server_id>/test", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_test(server_id: uuid.UUID) -> ResponseReturnValue:
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(test_connection(srv))


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
		return jsonify(test_user(srv, username, password))


@bp.route("/ldap/<uuid:server_id>/fetch_dn", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_fetch_dn(server_id: uuid.UUID) -> ResponseReturnValue:
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(fetch_base_dn(srv))


@bp.route("/ldap/<uuid:server_id>/explore", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_explore(server_id: uuid.UUID) -> ResponseReturnValue:
	dn = request.form.get("dn", "").strip() or None
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(walk_tree(srv, dn))


@bp.route("/ldap/<uuid:server_id>/import", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_import(server_id: uuid.UUID) -> ResponseReturnValue:
	"""Users and groups picked in the directory browser: users become LDAP
	accounts (approved, active), groups become mappable groups; ones that
	exist are skipped, and so are malformed items. JSON [{type: user | group,
	username | dn, label}].

	:returns: {users_created, groups_created, skipped}; 422 when the body
	 isn't a list"""
	items = request.get_json(silent=True)
	if items is None:
		items = []
	if not isinstance(items, list):
		return err("The import is a list of users and groups.", 422)
	users_created = 0
	groups_created = 0
	skipped = 0

	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)

		for item in items:
			if not isinstance(item, dict) or not (
					(item.get("type") == "user" and item.get("username"))
					or (item.get("type") == "group" and item.get("dn"))):
				skipped += 1
				continue
			if item["type"] == "user":
				if db_session.query(User).filter_by(
						username=item["username"]).first():
					skipped += 1
					continue
				db_session.add(User(
					username=item["username"],
					auth_type="ldap",
					ldap_server_id=server_id,
					is_approved=True,
					is_active=True,
					password_hash=None
				))
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

		current_app.web.audit("admin.ldap_import", success=True,
		                      detail={"users": users_created,
		                              "groups": groups_created,
		                              "skipped": skipped})

	return ok(users_created=users_created, groups_created=groups_created,
	          skipped=skipped)


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
		current_app.web.audit("admin.ldap_group_toggle",
		                      object_type="LDAPGroup",
		                      object_label=g.label, success=True,
		                      detail={"is_active": g.is_active})
	return ok(is_active=new_state)


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
		current_app.web.audit("admin.ldap_group_delete",
		                      object_type="LDAPGroup",
		                      object_label=g.label, success=True)
	return ok()
