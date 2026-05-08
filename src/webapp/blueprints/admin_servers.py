# python utilities
import os
import subprocess
import sys
import threading
import time

# services
# flask
from flask import Blueprint, render_template, request, current_app, jsonify, \
	Response
from flask_login import login_required
from redis.exceptions import ConnectionError as RedisConnectionError
# redis
import redis as redis_lib
# sqlalchemy
from sqlalchemy import text, create_engine
from sqlalchemy.exc import OperationalError

# local modules
from src.db.postgres_db import PostgresConfig
from src.db.redis_db import RedisConfig
from src.db.tables import LDAPServer, LDAPGroup, User
from src.encryption import encrypt
from src.ldap_auth import test_user, test_connection, fetch_base_dn, walk_tree
from src.webapp.utils import ok, err, require_admin, with_json, with_form

bp = Blueprint('admin_servers', __name__, url_prefix='/admin/server')


##############################Route Helpers#####################################
def unload_postgres_data(data):
	host, port, name = (data.get("host", "").strip(),
	                    data.get("port", "5432").strip(),
	                    data.get("name", "").strip())
	user, password, schema = (data.get("user", "").strip(),
	                          data.get("password", "").strip(),
	                          data.get("schema", "").strip())
	if not all([host, port, name, user, password]):
		return err("All fields except schema are required")
	if (host == current_app.backend.postgres.engine.url.host and
			str(port) == str(current_app.backend.postgres.engine.url.port) and
			name == current_app.backend.postgres.engine.url.database):
		return err("Target is the same as the current database")
	return {"host": host,
	        "port": port,
	        "name": name,
	        "user": user,
	        "password": password,
	        "schema": schema
	        }


def unload_ldap_data(ldap_request):
	label = ldap_request.form.get("label", "").strip()
	ip = ldap_request.form.get("ip", "").strip()
	port = ldap_request.form.get("port", "389").strip()
	base_dn = ldap_request.form.get("base_dn", "").strip()
	cn_identifier = ldap_request.form.get("cn_identifier", "").strip()
	bind_type = ldap_request.form.get("bind_type", "").strip()
	bind_dn = ldap_request.form.get("bind_dn", "").strip()
	use_ssl = ldap_request.form.get("use_ssl", "").strip()
	is_active = ldap_request.form.get("is_active", "").strip()
	bind_password = ldap_request.form.get("bind_password", "").strip()

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


##############################Routes#######################################

@bp.route("")
@login_required
@require_admin
def admin_server():
	try:
		with current_app.backend.postgres.engine.connect() as conn:
			conn.execute(text("SELECT 1"))
		db_connected = True
	except OperationalError:
		db_connected = False
	try:
		current_app.backend.redis.client.ping()
		redis_connected = True
	except RedisConnectionError:
		redis_connected = False
	#TODO check DB optional flags
	# TODO enforce case convention for DB config
	connection_modes = current_app.backend.connection_modes()
	return render_template('server_management.html',
	                       active_section="server",
	                       db_mode=connection_modes["POSTGRES"],
	                       db_connected=db_connected,
	                       db_host=current_app.backend.postgres.config
	                       .host,
	                       db_port=current_app.backend.postgres.config.port,
	                       db_name=current_app.backend.postgres.config.database,
	                       db_user=current_app.backend.postgres.config.user,
	                       postgres_schema=current_app.backend.postgres
	                       .config.schema,
	                       redis_mode=connection_modes["REDIS"],
	                       redis_connected=redis_connected,
	                       redis_host=current_app.backend.redis.config.host,
	                       redis_port=current_app.backend.redis.config.port,
	                       redis_db=current_app.backend.redis.config.db)


@bp.route("/postgres/test", methods=["POST"])
@login_required
@require_admin
@with_json()
def admin_server_postgres_test(data):
	server_input = unload_postgres_data(data)
	if isinstance(server_input, Response):
		return server_input

	url = (f"postgresql+psycopg2://{server_input["user"]}:"
	       f"{server_input["password"]}@{server_input["host"]}"
	       f":{server_input["port"]}/{server_input["name"]}")
	connect_args = {"options": f"-c search_path={server_input["schema"]}"}\
		if server_input["schema"] else {}

	try:
		test_engine = create_engine(url, connect_args=connect_args)
		with test_engine.connect() as conn:
			conn.execute(text("SELECT 1"))
		test_engine.dispose()
		return ok("Connection successful")
	except OperationalError as e:
		return err(str(e))


@bp.route("/postgres/save", methods=["POST"])
@login_required
@require_admin
@with_json()
def admin_server_postgres_save(data):
	server_input = unload_postgres_data(data)
	if isinstance(server_input, Response):
		return server_input

	new_config = PostgresConfig(
		host=server_input["host"], port=server_input["port"],
		database=server_input["name"],user=server_input["user"],
		password=server_input["password"], schema=server_input["schema"] or None
	)
	try:
		current_app.backend.reload_postgres(new_config)
		return ok("Configuration saved")
	except RuntimeError as e:
		return err(str(e))


@bp.route("/redis/test", methods=["POST"])
@login_required
@require_admin
@with_json("host")
def admin_server_redis_test(data):
	host = data.get("host", "").strip()
	port = data.get("port", "6379").strip()
	password = data.get("password", "").strip()
	db = data.get("db", "0").strip()
	try:
		test_client = redis_lib.Redis(
			host=host, port=int(port), db=int(db or 0),
			password=password or None, socket_connect_timeout=5
		)
		test_client.ping()
		test_client.close()
		return ok("Connection successful")
	except Exception as e:
		return err(str(e))


@bp.route("/redis/save", methods=["POST"])
@login_required
@require_admin
@with_json("host")
def admin_server_redis_save(data):
	host = data.get("host", "").strip()
	port = data.get("port", "6379").strip()
	password = data.get("password", "").strip()
	db = data.get("db", "0").strip()
	if (host == current_app.backend.redis.config.host and
			str(port) == str(current_app.backend.redis.config.port) and
			str(db or "0") == str(current_app.backend.redis.config.db)):
		return err("Target is the same as the current Redis instance")

	new_config = RedisConfig(host=host, port=port, db=db or "0",
	                         password=password or None)
	try:
		current_app.backend.reload_redis(new_config)
		return ok("Configuration saved")
	except RuntimeError as e:
		return err(str(e))


@bp.route("/ldap", methods=["GET"])
@login_required
@require_admin
def admin_server_ldap_get():
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
def admin_server_ldap_new():
	server_input = unload_ldap_data(request)

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
def admin_server_ldap_save(server_id):
	server_input = unload_ldap_data(request)

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
def admin_server_ldap_delete(server_id):
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
def admin_server_ldap_test(server_id):
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(test_connection(srv))


@bp.route("/ldap/<uuid:server_id>/test_user", methods=["POST"])
@login_required
@require_admin
@with_form("username", "password")
def admin_server_ldap_test_user(server_id, data):
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
def admin_server_ldap_fetch_dn(server_id):
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(fetch_base_dn(srv))


@bp.route("/ldap/<uuid:server_id>/explore", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_explore(server_id):
	dn = request.form.get("dn", "").strip() or None
	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)
		return jsonify(walk_tree(srv, dn))


@bp.route("/ldap/<uuid:server_id>/import", methods=["POST"])
@login_required
@require_admin
def admin_server_ldap_import(server_id):
	items = request.json or []
	users_created = 0
	groups_created = 0
	skipped = 0

	with current_app.backend.postgres.get_session() as db_session:
		srv = db_session.query(LDAPServer).filter_by(id=server_id).first()
		if not srv:
			return err("Server not found", 404)

		for item in items:
			if item["type"] == "user":
				exists = db_session.query(User).filter_by(
					username=item["username"]).first()
				if exists:
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
				exists = db_session.query(LDAPGroup).filter_by(
					group_dn=item["dn"], ldap_server_id=server_id).first()
				if exists:
					skipped += 1
					continue
				db_session.add(LDAPGroup(
					group_dn=item["dn"],
					label=item["label"],
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
def admin_server_ldap_groups(server_id):
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
def admin_server_ldap_group_toggle(server_id, group_id):
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
def admin_server_ldap_group_delete(server_id, group_id):
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

#TODO check url prefix corrnss across blueprints
@bp.route("/restart", methods=["POST"])
@login_required
@require_admin
def admin_restart():
	#TODO fix exit
	current_app.web.audit("server.restart", object_type="Server", object_label="webapp")

	def _do_restart():
		time.sleep(1.5)
		subprocess.Popen([sys.executable] + sys.argv)
		os._exit(0)

	threading.Thread(target=_do_restart, daemon=True).start()
	return ok()
#TODO remove restart button when packaging docker