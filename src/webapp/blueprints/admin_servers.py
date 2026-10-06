import os
import time

import redis as redis_lib
from flask import Blueprint, render_template, request, jsonify
from flask_login import current_user, login_required
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from src.db import move
from src.db.postgres_db import PostgresConfig
from src.db.redis_db import RedisConfig
from src.db.tables import LDAPServer, LDAPGroup, User
from src.encryption import encrypt
from src.ldap_auth import test_user, test_connection, fetch_base_dn, walk_tree
from src.runtime import drain_seconds
from src.webapp import db_move, proxy_config
from src.webapp.blueprints.auth import record_redis_session
from src.webapp.db_move import describe, same_database
from src.webapp.flask_app import current_app
from src.webapp.maintenance import during_maintenance
from src.webapp.setup import clear_sessions, clear_stale_jobs
from src.webapp.utils import ok, err, require_admin, with_json, with_form

bp = Blueprint('admin_servers', __name__, url_prefix='/admin/server')


##############################Route Helpers#####################################
def unload_ldap_data(req):
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
	                       db_place=describe(current_app.backend.postgres.config),
	                       can_move_back=(connection_modes["POSTGRES"] == "external"
	                                      and current_app.backend.bundled_postgres() is not None),
	                       access_needed=move.ACCESS_NEEDED,
	                       db_move=_status(),
	                       redis_mode=connection_modes["REDIS"],
	                       redis_place="{}:{}/{}".format(*current_app.backend.redis.config.place()),
	                       can_redis_back=(connection_modes["REDIS"] == "external"
	                                       and current_app.backend.bundled_redis() is not None),
	                       redis_connected=redis_connected,
	                       redis_host=current_app.backend.redis.config.host,
	                       redis_port=current_app.backend.redis.config.port,
	                       redis_db=current_app.backend.redis.config.db,
	                       access=proxy_config.overview(
		                       current_app.backend.settings.get("public_hostname")))


# ── Database: move to another server / back (src/db/move.py, db_move.py) ────

def _target(data, user_key="user", password_key="password"):
	"""A PostgresConfig from the form, or an error response."""
	host, port = data.get("host", "").strip(), str(data.get("port") or "5432").strip()
	database, schema = data.get("database", "").strip(), data.get("schema", "").strip()
	user, password = data.get(user_key, "").strip(), data.get(password_key, "")
	if not all([host, port, database, user, password]):
		return err("Fill in the server, port, database, login and password.", 422)
	if not port.isdigit():
		return err("The port is a number.", 422)
	return PostgresConfig(host=host, port=port, database=database, user=user,
	                      password=password, schema=None if schema in ("", "public") else schema)


def _plan(data) -> move.Plan:
	return move.Plan(database=data.get("database", "").strip() or move.DEFAULT_DATABASE,
	                 schema=data.get("schema", "").strip() or "public",
	                 login=data.get("login", "").strip() or move.DEFAULT_LOGIN,
	                 grafana_password=os.environ.get("GRAFANA_DB_PASSWORD") or None)


@bp.route("/database/sql", methods=["POST"])
@login_required
@require_admin
@with_json()
def database_sql(data):
	"""The DBA's way: what to ask for, and the SQL - a new password for the
	app's login, Grafana's (when this server knows it) filled in."""
	plan = _plan(data)
	return ok(sql=move.preparation_sql(plan), access=list(move.ACCESS_NEEDED),
	          database=plan.database, schema=plan.schema, login=plan.login,
	          password=plan.password, grafana_known=bool(plan.grafana_password))


@bp.route("/database/prepare", methods=["POST"])
@login_required
@require_admin
@with_json()
def database_prepare(data):
	"""The administrator-login way: the same created now; the admin login is
	used for this request only, never stored or logged."""
	admin = _target({**data, "database": "postgres"}, "admin_user", "admin_password")
	if not isinstance(admin, PostgresConfig):
		return admin
	plan = _plan(data)
	label = f"{admin.host}:{admin.port}/{plan.database}"
	try:
		done = move.prepare_with_admin(admin.host, admin.port, admin.user, admin.password, plan)
	except move.MoveError as e:
		current_app.web.audit("database.prepare_failed", object_type="database",
		                      object_label=label, success=False, detail={"message": str(e)})
		return err(str(e))
	current_app.web.audit("database.prepared", object_type="database", object_label=label,
	                      detail={"login": plan.login, "schema": plan.schema, "done": done})
	return ok(done=done, database=plan.database, schema=plan.schema, login=plan.login,
	          password=plan.password, grafana_known=bool(plan.grafana_password))


@bp.route("/database/check", methods=["POST"])
@login_required
@require_admin
@with_json()
def database_check(data):
	target = _target(data)
	if not isinstance(target, PostgresConfig):
		return target
	if same_database(target, current_app.backend.postgres.config):
		return err("That is the database NetRollout uses now.")
	return ok(report=move.check_target(target).as_dict())


@bp.route("/database/move", methods=["POST"])
@login_required
@require_admin
@with_json()
def database_move(data):
	target = _target(data)
	if not isinstance(target, PostgresConfig):
		return target
	return _start(target, back=False)


@bp.route("/database/move-back", methods=["POST"])
@login_required
@require_admin
def database_move_back():
	bundled = current_app.backend.bundled_postgres()
	if bundled is None or current_app.backend.connection_modes()["POSTGRES"] == "bundled":
		return err("There's no bundled database to move back to.", 409)
	return _start(bundled, back=True)


def _start(target: PostgresConfig, back: bool):
	try:
		current_app.db_move.start(target, current_user.id, current_user.username, back=back)
	except move.MoveError as e:
		return err(str(e), 409)
	current_app.web.audit("database.move_started", object_type="database",
	                      object_label=describe(target), detail={"back": back})
	return ok(move=_status())


@bp.route("/database/move/status")
@during_maintenance            # read only: the moving admin's page follows it
@login_required
@require_admin
def database_move_status():
	return ok(move=_status())


def _status() -> dict:
	"""The move's state and step; while it waits, the rollouts it waits for."""
	status = current_app.db_move.status()
	status.pop("deadline", None)
	if status["state"] == db_move.WAITING:
		status["seconds_left"] = max(0, int(current_app.db_move.seconds_left()))
		jobs = current_app.orchestrator.jobs()
		names = {}
		if jobs:
			with current_app.backend.postgres.get_session() as session:
				names = dict(session.query(User.id, User.username)
				             .filter(User.id.in_({j["user_id"] for j in jobs})).all())
		status["rollouts"] = [{**j, "job_id": str(j["job_id"]), "user_id": str(j["user_id"]),
		                       "user": names.get(j["user_id"], "?")} for j in jobs]
	return status


@bp.route("/database/move/cancel", methods=["POST"])
@login_required
@require_admin
def database_move_cancel():
	if not current_app.db_move.cancel():
		return err("The move can be cancelled only while it waits for rollouts.", 409)
	return ok("Cancelling")


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
	return _switch_redis(new_config, back=False)


@bp.route("/redis/back", methods=["POST"])
@login_required
@require_admin
def admin_server_redis_back():
	bundled = current_app.backend.bundled_redis()
	if bundled is None or current_app.backend.connection_modes()["REDIS"] == "bundled":
		return err("There's no bundled Redis to switch back to.", 409)
	return _switch_redis(bundled, back=True)


def _switch_redis(config: RedisConfig, back: bool):
	"""Live, no restart: everything looks the client up per use. Refused
	while rollouts run (their live state is in Redis). Sessions and leftover
	job state are cleared in the Redis switched to - one used before still
	holds old sessions, terminated ones included - so everyone signs in
	again; the admin who switches stays signed in (this request saves its
	session into the new Redis at its end - its index entry goes there now)."""
	if any(current_app.orchestrator.counts().values()):
		return err("Rollouts are running - their live state is in Redis. Switch "
		           "once they've finished.", 409)
	try:
		current_app.backend.reload_redis(config)
	except RuntimeError as e:
		return err(str(e))
	clear_sessions(current_app.backend.redis)
	clear_stale_jobs(current_app.backend.redis)
	record_redis_session(current_user.id)
	place = "{}:{}/{}".format(*config.place())
	current_app.web.audit("server.redis_switched", object_type="redis", object_label=place,
	                      detail={"back": back})
	return ok(f"NetRollout now uses Redis at {place}. Everyone else signs in again.")


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

# a PEM certificate chain or key is a few KB; anything bigger isn't one
CERT_UPLOAD_MAX_BYTES = 256 * 1024


def _certificate_applied(action: str, undo, started: float, managed: bool,
                         detail: dict):
	"""After the files are written: nginx's verdict — rejected → the previous
	files are put back and the reason returned; otherwise audited and the
	new status returned."""
	settings = current_app.backend.settings
	proxy = proxy_config.verdict(managed, started)
	if proxy["state"] == "rejected":
		undo()
		return err(f"nginx rejected the certificate: {proxy.get('message')} — "
		           f"the previous certificate is back.", 422)
	current_app.web.audit(action, object_type="Certificate",
	                      object_label=", ".join(detail.get("names", [])[:3]),
	                      detail={**detail, "nginx": proxy["state"]})
	return ok(proxy=proxy,
	          access=proxy_config.overview(settings.get("public_hostname")))


@bp.route("/certificate", methods=["POST"])
@login_required
@require_admin
def certificate_upload():
	"""An organisation's certificate (+ chain) and key: checked, then used —
	all or nothing, nginx's verdict included."""
	files = {}
	for field in ("certificate", "key"):
		upload = request.files.get(field)
		if not upload or not upload.filename:
			return err("Choose both files: the certificate and its private key.")
		data = upload.read(CERT_UPLOAD_MAX_BYTES + 1)
		if len(data) > CERT_UPLOAD_MAX_BYTES:
			return err(f"The {field} file is too big for a PEM {field}.")
		files[field] = data
	hostname = current_app.backend.settings.get("public_hostname")
	managed = proxy_config.read_status() is not None
	started = time.time()
	try:
		check, undo = proxy_config.install_certificate(
			files["certificate"], files["key"], hostname)
	except proxy_config.ProxyError as e:
		return err(f"Not used: {e}", 422)
	return _certificate_applied(
		"server.certificate_uploaded", undo, started, managed,
		{"subject": check.subject, "names": check.names,
		 "not_after": check.not_after.isoformat() if check.not_after else None,
		 "warnings": check.warnings})


@bp.route("/certificate/selfsigned", methods=["POST"])
@login_required
@require_admin
def certificate_selfsigned():
	"""A new self-signed certificate for the saved hostname (D2)."""
	hostname = current_app.backend.settings.get("public_hostname")
	managed = proxy_config.read_status() is not None
	started = time.time()
	try:
		undo = proxy_config.generate_selfsigned(hostname)
	except proxy_config.ProxyError as e:
		return err(str(e), 422)
	names = (proxy_config.overview(hostname)["certificate"] or {}).get("names", [])
	return _certificate_applied("server.certificate_generated", undo, started,
	                            managed, {"names": names})


@bp.route("/restart", methods=["POST"])
@login_required
@require_admin
def admin_restart():
	"""Restart NetRollout. With rollouts running or queued the caller must
	choose (409 otherwise): "when_finished" drains — new rollouts paused,
	running ones finish (up to the drain deadline) — "now" cancels them.
	Queued rollouts are recorded as cancelled either way."""
	mode = (request.get_json(silent=True) or {}).get("mode")
	if mode not in (None, "when_finished", "now"):
		return err("mode must be when_finished or now", 422)
	counts = current_app.orchestrator.counts()
	if (counts["running"] or counts["queued"]) and mode is None:
		return err("Rollouts are running — restart when they finish, or now "
		           "(cancels them)?", 409, **counts)
	deadline = 0 if mode == "now" else drain_seconds()
	if not current_app.shutdown.begin(deadline, restart=True):
		return err("A restart is already in progress", 409)
	current_app.web.audit("server.restart", object_type="Server",
	                      object_label="webapp",
	                      detail={"mode": mode or "idle", **counts})
	return ok(**counts)