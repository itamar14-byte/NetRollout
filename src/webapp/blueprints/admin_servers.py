"""Server Management: the database (move to another server and back), Redis
(a live switch and back), LDAP servers and their groups, the TLS
certificate, and Restart. Admins only; every change audited."""
import os
import time
from collections.abc import Callable
from typing import Any

import redis as redis_lib
from flask import Blueprint, Response, render_template, request
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from src.access import nginx
from src.accounts.users import clear_sessions
from src.db import move
from src.db.connections import PostgresConfig, REDIS_UNAVAILABLE, RedisConfig
from src.jobs import clear_stale_jobs, with_owners
from src.runtime import drain_seconds
from src.webapp import db_move
from src.webapp.app import current_app
from src.webapp.db_move import describe, same_database, during_maintenance
from src.webapp.http import ok, err, require_admin, with_json


bp = Blueprint('admin_servers', __name__, url_prefix='/admin/server')


@bp.route("")
@login_required
@require_admin
def admin_server() -> str:
	"""Server Management: each service's place and state, the move's
	progress, the certificate."""
	try:
		with current_app.backend.postgres.engine.connect() as conn:
			conn.execute(text("SELECT 1"))
		db_connected = True
	except OperationalError:
		db_connected = False
	try:
		current_app.backend.redis.client.ping()
		redis_connected = True
	except REDIS_UNAVAILABLE:            # refused, or no answer at all
		redis_connected = False
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
	                       access=nginx.overview(
		                       current_app.backend.settings.get("public_hostname")))


# ── Database: move to another server / back (src/db/move.py, db_move.py) ────

def _target(data: dict[str, Any], user_key: str = "user",
            password_key: str = "password") -> PostgresConfig | tuple[Response, int]:
	"""A PostgresConfig from the form ("public" or no schema: none).

	:param user_key: the form's field for the login (and password_key, its
	 password) - the app's, or the administrator's
	:returns: the config; or 422 - something missing, or the port not a number"""
	host, port = data.get("host", "").strip(), str(data.get("port") or "5432").strip()
	database, schema = data.get("database", "").strip(), data.get("schema", "").strip()
	user, password = data.get(user_key, "").strip(), data.get(password_key, "")
	if not all([host, port, database, user, password]):
		return err("Fill in the server, port, database, login and password.", 422)
	if not port.isdigit():
		return err("The port is a number.", 422)
	return PostgresConfig(host=host, port=port, database=database, user=user,
	                      password=password, schema=None if schema in ("", "public") else schema)


def _plan(data: dict[str, Any]) -> move.Plan:
	""":returns: what to prepare on the new server - its database, schema and
	 login (defaults where blank), Grafana's password when this server knows it"""
	return move.Plan(database=data.get("database", "").strip() or move.DEFAULT_DATABASE,
	                 schema=data.get("schema", "").strip() or "public",
	                 login=data.get("login", "").strip() or move.DEFAULT_LOGIN,
	                 grafana_password=os.environ.get("GRAFANA_DB_PASSWORD") or None)


@bp.route("/database/sql", methods=["POST"])
@login_required
@require_admin
@with_json()
def database_sql(data: dict[str, Any]) -> Response:
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
def database_prepare(data: dict[str, Any]) -> ResponseReturnValue:
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
def database_check(data: dict[str, Any]) -> ResponseReturnValue:
	"""Check a server before a move (move.check_target): the report."""
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
def database_move(data: dict[str, Any]) -> ResponseReturnValue:
	"""Start a move to the server in the form (checked first)."""
	target = _target(data)
	if not isinstance(target, PostgresConfig):
		return target
	return _start(target, back=False)


@bp.route("/database/move-back", methods=["POST"])
@login_required
@require_admin
def database_move_back() -> ResponseReturnValue:
	"""Start a move back to the bundled database (its address remembered by
	the move away)."""
	bundled = current_app.backend.bundled_postgres()
	if bundled is None or current_app.backend.connection_modes()["POSTGRES"] == "bundled":
		return err("There's no bundled database to move back to.", 409)
	return _start(bundled, back=True)


def _start(target: PostgresConfig, back: bool) -> ResponseReturnValue:
	""":returns: the move's status once started (db_move audits it); 409 when
	 refused"""
	try:
		current_app.db_move.start(target, current_user.id, current_user.username, back=back)
	except move.MoveError as e:
		return err(str(e), 409)
	return ok(move=_status())


@bp.route("/database/move/status")
@during_maintenance            # read only: the moving admin's page follows it
@login_required
@require_admin
def database_move_status() -> Response:
	"""The move's progress, polled by the page - also while locked."""
	return ok(move=_status())


def _status() -> dict[str, Any]:
	"""The move's state and step; while it waits, the rollouts it waits for."""
	status = current_app.db_move.status()
	status.pop("deadline", None)
	if status["state"] == db_move.WAITING:
		status["seconds_left"] = max(0, int(current_app.db_move.seconds_left()))
		status["rollouts"] = _rollouts()
	return status


def _rollouts() -> list[dict[str, Any]]:
	""":returns: this process's rollouts, each with its owner's name
	 (jobs.with_owners)"""
	return with_owners(current_app.orchestrator.jobs(), current_app.backend.postgres)


@bp.route("/database/move/cancel", methods=["POST"])
@login_required
@require_admin
def database_move_cancel() -> ResponseReturnValue:
	"""Give the move up - only while it waits for rollouts."""
	if not current_app.db_move.cancel():
		return err("The move can be cancelled only while it waits for rollouts.", 409)
	return ok("Cancelling")


@bp.route("/redis/test", methods=["POST"])
@login_required
@require_admin
@with_json("host")
def admin_server_redis_test(data: dict[str, Any]) -> ResponseReturnValue:
	"""Can NetRollout reach this Redis (a PING)? JSON {host, port, password, db}."""
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
def admin_server_redis_save(data: dict[str, Any]) -> ResponseReturnValue:
	"""Switch to another Redis (_switch_redis)."""
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
def admin_server_redis_back() -> ResponseReturnValue:
	"""Switch back to the bundled Redis."""
	bundled = current_app.backend.bundled_redis()
	if bundled is None or current_app.backend.connection_modes()["REDIS"] == "bundled":
		return err("There's no bundled Redis to switch back to.", 409)
	return _switch_redis(bundled, back=True)


def _switch_redis(config: RedisConfig, back: bool) -> ResponseReturnValue:
	"""Live, no restart: everything looks the client up per use. Refused
	while rollouts run (their live state is in Redis). Sessions and leftover
	job state are cleared in the Redis switched to - one used before still
	holds old sessions, terminated ones included - so everyone signs in
	again; the admin who switches stays signed in (this request saves its
	session into the new Redis at its end - its index entry goes there now).
	New rollouts are paused from the check to the switch, so none starts in
	between (a pause already on - a database move's - is left on)."""
	orchestrator = current_app.orchestrator
	was_paused = orchestrator.paused
	orchestrator.pause()
	try:
		if any(orchestrator.counts().values()):
			return err("Rollouts are running - their live state is in Redis. Switch "
			           "once they've finished.", 409)
		try:
			current_app.backend.reload_redis(config)
		except RuntimeError as e:
			return err(str(e))
		clear_sessions(current_app.backend.redis)
		clear_stale_jobs(current_app.backend.redis)   # before a new rollout's keys go there
	finally:
		if not was_paused:
			orchestrator.resume()
	place = "{}:{}/{}".format(*config.place())
	current_app.web.audit("server.redis_switched", object_type="redis", object_label=place,
	                      detail={"back": back})
	return ok(f"NetRollout now uses Redis at {place}. Everyone else signs in again.")

# a PEM certificate chain or key is a few KB; anything bigger isn't one
CERT_UPLOAD_MAX_BYTES = 256 * 1024


def _certificate_applied(action: str, undo: Callable[[], None], started: float,
                         managed: bool, detail: dict[str, Any]) -> ResponseReturnValue:
	"""After the files are written: nginx's verdict — rejected → the previous
	files are put back and the reason returned; otherwise audited and the
	new status returned.

	:param started: when the files were written (the verdict comes after)
	:param managed: whether a NetRollout nginx reports here"""
	settings = current_app.backend.settings
	proxy = nginx.verdict(managed, started)
	if proxy["state"] == "rejected":
		undo()
		return err(f"nginx rejected the certificate: {proxy.get('message')} — "
		           f"the previous certificate is back.", 422)
	current_app.web.audit(action, object_type="Certificate",
	                      object_label=", ".join(detail.get("names", [])[:3]),
	                      detail={**detail, "nginx": proxy["state"]})
	return ok(proxy=proxy,
	          access=nginx.overview(settings.get("public_hostname")))


@bp.route("/certificate", methods=["POST"])
@login_required
@require_admin
def certificate_upload() -> ResponseReturnValue:
	"""An organisation's certificate (+ chain) and key: checked, then used —
	all or nothing, nginx's verdict included."""
	files: dict[str, bytes] = {}
	for field in ("certificate", "key"):
		upload = request.files.get(field)
		if not upload or not upload.filename:
			return err("Choose both files: the certificate and its private key.")
		data = upload.read(CERT_UPLOAD_MAX_BYTES + 1)
		if len(data) > CERT_UPLOAD_MAX_BYTES:
			return err(f"The {field} file is too big for a PEM {field}.")
		files[field] = data
	hostname = current_app.backend.settings.get("public_hostname")
	managed = nginx.read_status() is not None
	started = time.time()
	try:
		check, undo = nginx.install_certificate(
			files["certificate"], files["key"], hostname)
	except nginx.ProxyError as e:
		return err(f"Not used: {e}", 422)
	return _certificate_applied(
		"server.certificate_uploaded", undo, started, managed,
		{"subject": check.subject, "names": check.names,
		 "not_after": check.not_after.isoformat() if check.not_after else None,
		 "warnings": check.warnings})


@bp.route("/certificate/selfsigned", methods=["POST"])
@login_required
@require_admin
def certificate_selfsigned() -> ResponseReturnValue:
	"""A new self-signed certificate for the saved hostname (D2)."""
	hostname = current_app.backend.settings.get("public_hostname")
	managed = nginx.read_status() is not None
	started = time.time()
	try:
		undo = nginx.generate_selfsigned(hostname)
	except nginx.ProxyError as e:
		return err(str(e), 422)
	names = (nginx.overview(hostname)["certificate"] or {}).get("names", [])
	return _certificate_applied("server.certificate_generated", undo, started,
	                            managed, {"names": names})


@bp.route("/rollouts")
@login_required
@require_admin
def rollouts() -> Response:
	"""The rollouts a Restart waits for - queued and running, with their owner,
	devices, state and start - polled by the Restart dialog (also while the
	server drains: nothing refuses requests then).

	:returns: {rollouts: [...], running, queued, draining}"""
	return ok(rollouts=_rollouts(), draining=current_app.orchestrator.draining,
	          **current_app.orchestrator.counts())


@bp.route("/restart", methods=["POST"])
@login_required
@require_admin
def admin_restart() -> ResponseReturnValue:
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
