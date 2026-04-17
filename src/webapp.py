import base64
import glob
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from io import BytesIO
from itertools import groupby
from pathlib import Path
from queue import Empty
from time import sleep

import pyotp
import qrcode
from dotenv import load_dotenv, dotenv_values
from flask import (redirect, Response, request, render_template, url_for, \
                   Flask, flash, session, jsonify, send_file)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (LoginManager, login_required,
                         logout_user, current_user, login_user)
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException, \
	NetmikoAuthenticationException
from sqlalchemy import text, create_engine, and_, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from waitress import serve
from werkzeug.security import generate_password_hash, check_password_hash

# Must run before DB modules are imported — they build the engine at
_CONFIG_ENV = Path(__file__).parent.parent / "config.env"
load_dotenv(_CONFIG_ENV, override=True)
from db.db import get_session, engine
from db.tables import User, DeviceResult, SecurityProfile, Inventory, \
	VariableMapping, RolloutSession, AuditLog, JobMetadata, PropertyDefinition
from db.db_install import install
import encryption
import input_parser
from core import RolloutOptions, Device

from input_parser import InputParser
from logging_utils import RolloutLogger, LOGS_DIR
from orchestration import RolloutOrchestrator
from validation import Validator

app = Flask(__name__, template_folder='../templates')
# SECRET_KEY rotates every restart so all user sessions are invalidated.
app.config["SECRET_KEY"] = secrets.token_urlsafe(32)

# Extract DB host:port from engine for use in error pages
_DB_HOST = engine.url.host
_DB_PORT = engine.url.port

# Vendor logo URLs from Simple Icons CDN — keyed by Netmiko device_type
_CDN = "https://cdn.simpleicons.org"
VENDOR_LOGOS = {
	'cisco_ios': f'{_CDN}/cisco',
	'cisco_xe': f'{_CDN}/cisco',
	'cisco_xr': f'{_CDN}/cisco',
	'cisco_nxos': f'{_CDN}/cisco',
	'juniper_junos': f'{_CDN}/junipernetworks',
	'arista_eos': f'{_CDN}/aristanetworks',
	'fortinet': f'{_CDN}/fortinet',
	'paloalto_panos': f'{_CDN}/paloaltonetworks',
	'aruba_aoscx': f'{_CDN}/arubanetworks',
	'checkpoint_gaia': f'{_CDN}/checkpoint',
	'hp_procurve': f'{_CDN}/hp',
	'hp_comware': f'{_CDN}/hp',
}
app.jinja_env.globals['VENDOR_LOGOS'] = VENDOR_LOGOS

SYSTEM_PROPERTIES = [
	{"name": "hostname", "label": "Hostname", "icon": "bi-type-h1",
	 "is_list": False},
	{"name": "loopback_ip", "label": "Loopback IP", "icon": "bi-hdd-network",
	 "is_list": False},
	{"name": "asn", "label": "ASN", "icon": "bi-diagram-3", "is_list": False},
	{"name": "mgmt_vrf", "label": "Management VRF", "icon": "bi-box",
	 "is_list": False},
	{"name": "mgmt_interface", "label": "Management Interface",
	 "icon": "bi-ethernet", "is_list": False},
	{"name": "site", "label": "Site", "icon": "bi-geo-alt", "is_list": False},
	{"name": "domain", "label": "Domain", "icon": "bi-globe2",
	 "is_list": False},
	{"name": "timezone", "label": "Timezone", "icon": "bi-clock",
	 "is_list": False},
	{"name": "vrfs", "label": "VRFs", "icon": "bi-layers", "is_list": True},
]

QUERY_DEVICE_RESULT_FIELDS = {
	"started_at": (
		DeviceResult.started_at,
		{"equal", "less_or_equal", "greater_or_equal"}),
	"device_type": (
		DeviceResult.device_type, {"equal", "not_equal"}),
	"status": (
		DeviceResult.status, {"equal", "not_equal"}),
	"commands_sent": (
		DeviceResult.commands_sent,
		{"equal", "not_equal", "greater_or_equal",
		 "less_or_equal"}),
	"device_ip": (
		DeviceResult.device_ip, {"equal", "contains", "begins_with"}),
}
DEVICE_RESULT_COLUMNS = ["job_id", "device_ip", "device_type",
                         "status",
                         "commands_sent", "commands_verified",
                         "started_at", "completed_at"]

QUERY_AUDIT_LOG_FIELDS = {
	"timestamp": (
		AuditLog.timestamp, {"equal", "less_or_equal", "greater_or_equal"}),
	"actor_username": (
		AuditLog.actor_username,
		{"equal", "not_equal", "contains", "begins_with"}),
	"action": (
		AuditLog.action, {"equal", "not_equal", "contains", "begins_with"}),
	"object_type": (
		AuditLog.object_type, {"equal", "not_equal"}),
	"success": (
		AuditLog.success, {"equal"}),
	"ip_address": (
		AuditLog.ip_address, {"equal", "contains", "begins_with"}),
}

AUDIT_LOG_COLUMNS = ["timestamp", "actor_username", "action",
                     "object_type",
                     "object_label", "success", "ip_address"]

QUERY_OPS = {
	"equal": lambda x, y: x == y,
	"not_equal": lambda x, y: x != y,
	"greater_or_equal": lambda x, y: x >= y,
	"less_or_equal": lambda x, y: x <= y,
	"contains": lambda x, y: x.ilike(f"%{y}%"),
	"begins_with": lambda x, y: x.ilike(f"{y}%"),
	"ends_with": lambda x, y: x.ilike(f"%{y}")
}


def get_property_defs(user_id):
	with get_session() as db_session:
		user_props = db_session.query(PropertyDefinition).filter_by(
			user_id=user_id).order_by(PropertyDefinition.name).all()
		user_defs = [{"name": p.name, "label": p.label, "icon": p.icon,
		              "is_list": p.is_list, "id": str(p.id)}
		             for p in user_props]
	return SYSTEM_PROPERTIES, user_defs


orchestrator = RolloutOrchestrator()

login_mng = LoginManager()
login_mng.init_app(app)
login_mng.login_view = "home"

conn_limit = Limiter(get_remote_address, app=app, default_limits=[],
                     storage_uri="memory://")

csrf = CSRFProtect(app)
_FLAG = Path(__file__).parent.parent / "pending_db_init.flag"
if _FLAG.exists():
	install()
	_FLAG.unlink()


@login_mng.user_loader
def load_user(user_id):
	with get_session() as db_session:
		try:
			user = db_session.get(User, uuid.UUID(user_id))
		except ValueError:
			return None
		if user:
			db_session.expunge(user)
		return user


def audit(action, *, object_type=None, object_id=None, object_label=None,
          detail=None, success=True, username=None, actor_id=None):
	"""Write one append-only audit row. Opens its own session so the write
	commits independently of the calling route transactido ion."""
	if username is None:
		username = current_user.username if current_user.is_authenticated else "anonymous"
	if actor_id is None:
		actor_id = current_user.id if current_user.is_authenticated else None
	with get_session() as db_session:
		db_session.add(AuditLog(
			actor_id=actor_id,
			actor_username=username,
			action=action,
			object_type=object_type,
			object_id=object_id,
			object_label=object_label,
			success=success,
			ip_address=request.remote_addr,
			detail=detail,
		))


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
	if request.is_json:
		return jsonify({"status": "error", "message": "Session expired"}), 400
	return redirect(url_for("home"))


@app.errorhandler(OperationalError)
def handle_db_unavailable(e):
	if request.is_json or request.path.startswith('/rollout_stream'):
		return jsonify({
			"status": "error",
			"message": f"Database unavailable — check {_DB_HOST}:{_DB_PORT}"
		}), 503
	return render_template("db_error.html", db_host=_DB_HOST,
	                       db_port=_DB_PORT), 503


@app.route("/")
def home():
	return render_template("index.html")


@app.route("/login", methods=["GET"])
def login_get():
	return redirect(url_for("home"))


@app.route("/login", methods=["POST"])
@csrf.exempt
@conn_limit.limit("10 per minute")
def login():
	# Origin check replaces CSRF for login — blocks cross-origin POSTs without
	# depending on session state, so it survives server restarts
	origin = request.headers.get("Origin") or request.headers.get("Referer", "")
	expected = request.host_url.rstrip("/")
	if origin and not origin.startswith(expected):
		return redirect(url_for("home"))
	username = request.form["username"]
	password = request.form["password"]
	with get_session() as db_session:
		user = db_session.query(User).filter_by(username=username).first()
		# checks credentials are correct
		if user and check_password_hash(user.password_hash, password):
			# checks user was activated
			if user.is_approved:
				if user.is_active:
					if user.username == "admin":
						db_session.expunge(user)
						login_user(user)
						audit("auth.login", success=True,
						      username=user.username, actor_id=user.id)
						return redirect(url_for("dashboard"))
					# checks otp enrollment
					elif user.otp_secret:
						session["pre_auth_user_id"] = str(user.id)
						audit("auth.login", success=True,
						      username=user.username, actor_id=user.id)
						return redirect(url_for("otp_verify"))
					else:
						flash("To complete enrollment,"
						      " you are referred to OPT set up portal"
						      , "info")
						session["pre_auth_user_id"] = str(user.id)
						audit("auth.login", success=True,
						      username=user.username, actor_id=user.id)
						return redirect(url_for("otp_enroll"))
				else:
					flash("User disabled, please check with administrator",
					      "danger")
					audit("auth.login", success=False,
					      username=user.username, actor_id=user.id,
					      detail={"reason": "account_disabled"})
					session.pop("pre_auth_user_id", None)
					return redirect(url_for("home"))
			else:
				flash("User still pending admin approval",
				      "danger")
				audit("auth.login", success=False,
				      username=user.username, actor_id=user.id,
				      detail={"reason": "pending_approval"})
				session.pop("pre_auth_user_id", None)
				return redirect(url_for("home"))
		else:
			flash("invalid credentials", "danger")
			audit("auth.login", success=False, username=username,
			      detail={"reason": "invalid_credentials"})
			session.pop("pre_auth_user_id", None)
			return redirect(url_for("home"))


@app.route("/register", methods=["GET"])
def register_form():
	return render_template("register.html")


@app.route("/register", methods=["POST"])
def register():
	username = request.form["username"]
	pass_hash = generate_password_hash(request.form["password"])
	email = request.form["email"]
	full_name = request.form["full_name"]
	position = request.form.get("position", None)

	new_user = User(username=username,
	                password_hash=pass_hash,
	                email=email,
	                full_name=full_name,
	                role="user",
	                position=position)

	with get_session() as db_session:
		try:
			db_session.add(new_user)
			db_session.flush()
			audit("auth.register", success=True, username=username,
			      object_type="User", object_label=username)
			flash("Registration successful -"
			      " your account is pending admin "
			      "approval.", "success")
			return redirect(url_for("home"))
		except IntegrityError:
			audit("auth.register", success=False, username=username,
			      detail={"reason": "duplicate_username_or_email"})
			flash("email or username already exists", "danger")
			return redirect(url_for("register_form"))


@app.route("/otp_enroll", methods=["GET", "POST"])
def otp_enroll():
	if request.method == "GET":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("home"))
		with get_session() as db_session:
			try:
				user = db_session.query(User).filter_by(
					id=uuid.UUID(user_id)).first()
			except ValueError:
				return redirect(url_for("home"))
			db_session.expunge(user)
		totp = pyotp.random_base32()
		secret = session.get("pending_totp_secret", None) or totp
		session["pending_totp_secret"] = secret
		uri = pyotp.TOTP(session["pending_totp_secret"]).provisioning_uri(
			user.username, issuer_name="NetRollout")
		img = qrcode.make(uri)
		buffer = BytesIO()
		img.save(buffer, format="png")
		buffer.seek(0)
		qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
		return render_template("otp_enroll.html", qr=qr_b64)

	if request.method == "POST":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("home"))
		otp_secret = session.get("pending_totp_secret", None)
		user_code = request.form["code"]
		if pyotp.TOTP(otp_secret).verify(user_code, valid_window=1):
			with get_session() as db_session:
				try:
					user = db_session.query(User).filter_by(id=uuid.UUID(
						user_id)).first()
				except ValueError:
					return redirect(url_for("home"))
				user.otp_secret = encryption.encrypt(otp_secret)
				db_session.flush()
				db_session.expunge(user)
			session.pop("pending_totp_secret")
			session.pop("pre_auth_user_id")
			login_user(user)
			return redirect(url_for("dashboard"))
		flash("invalid code, please try again", "danger")
		return redirect(url_for("otp_enroll"))


@app.route("/otp_verify", methods=["GET", "POST"])
def otp_verify():
	if request.method == "POST":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("home"))
		user_code = request.form["code"]
		with get_session() as db_session:
			try:
				user = db_session.query(User).filter_by(
					id=uuid.UUID(user_id)).first()
			except ValueError:
				return redirect(url_for("home"))
			db_session.expunge(user)
		if not user.otp_secret:
			return redirect(url_for("otp_enroll"))
		if pyotp.TOTP(encryption.decrypt(user.otp_secret)).verify(user_code,
		                                                          valid_window=1):
			login_user(user)
			session.pop("pre_auth_user_id")
			return redirect(url_for("dashboard"))
		flash("invalid code, please try again", "danger")
		return redirect(url_for("otp_verify"))
	elif request.method == "GET":
		return render_template("otp_verify.html")


@app.route("/logout")
def logout():
	audit("auth.logout")
	logout_user()
	return redirect(url_for("home"))


@app.route("/account")
@login_required
def account():
	with get_session() as db_session:
		user = db_session.get(User, current_user.id)
		user_results = user.results
		db_session.expunge_all()

	# Total rollouts
	total_rollouts = len(set(r.job_id for r in user_results))

	# Total devices configured
	total_devices = len(user_results)

	# Success rate
	if total_rollouts > 0:
		successful = len(
			set(r.job_id for r in user_results if r.status == 'success'))
		success_rate = round((successful / total_rollouts) * 100)
	else:
		success_rate = None

	# Most configured device type
	if user_results:
		most_common_platform = \
			Counter(r.device_type for r in user_results).most_common(1)[0][0]
	else:
		most_common_platform = None

	# Total commands pushed
	total_commands = sum(r.commands_sent for r in user_results)

	return render_template("account.html",
	                       user=current_user,
	                       total_rollouts=total_rollouts,
	                       total_devices=total_devices,
	                       success_rate=success_rate,
	                       most_common_platform=most_common_platform,
	                       total_commands=total_commands)


@app.route("/cancel_rollout", methods=["POST"])
@login_required
def cancel_rollout():
	raw = request.form.get("job_id", "").strip()
	if not raw:
		return {"status": "no_active_rollout"}
	try:
		job_id = uuid.UUID(raw)
	except ValueError:
		return {"status": "invalid_job_id"}
	job = orchestrator.get(job_id)
	if not job:
		return {"status": "job_not_found"}
	if job.user_id != current_user.id and current_user.role != "admin":
		return {"status": "job_not_found_under_user"}
	orchestrator.cancel(job_id)
	audit("rollout.cancel", object_id=job_id)
	return {"status": "cancelled"}


@app.route("/new_rollout")
@login_required
def new_rollout():
	with get_session() as db_session:
		user = db_session.get(User, current_user.id)
		devices = user.inventory
		_ = [d.security_profile for d in devices]
		_ = [d.var_mappings for d in devices]
		db_session.expunge_all()

	return render_template("new_rollout.html",
	                       devices=devices,
	                       active_section="rollout"
	                       )


@app.route("/new_start_rollout", methods=["POST"])
@login_required
def new_start_rollout():
	# 1) Collect selected device IDs from the form.
	# The frontend sends repeated "device_ids" fields for the chosen inventory rows.
	raw_device_ids = request.form.getlist("device_ids")
	if not raw_device_ids:
		flash("Select at least one device.", "danger")
		return redirect(url_for("new_rollout"))

	# 2) Validate that the submitted device IDs are well-formed UUIDs.
	# This protects the backend from malformed or tampered requests.
	try:
		selected_ids = list(
			{uuid.UUID(device_id) for device_id in raw_device_ids})
	except ValueError:
		flash("Invalid device selection.", "danger")
		return redirect(url_for("new_rollout"))

	# 3) Detect single vs. multi-platform mode early.
	# The frontend sends a JSON field "platform_commands" when multiple platforms
	# are selected, and omits it (or sends empty) for single-platform rollouts.
	raw_platform_commands = request.form.get("platform_commands", "").strip()
	is_multi_platform = bool(raw_platform_commands)

	# 4) Collect and validate commands based on mode.
	if is_multi_platform:
		# Multi-platform: parse the JSON map of the platform → command text.
		try:
			platform_commands_map = json.loads(raw_platform_commands)
		except json.JSONDecodeError:
			flash("Invalid platform commands format.", "danger")
			return redirect(url_for("new_rollout"))
		# commands is unused in multi-platform mode — each platform has its own
		commands = None
	else:
		# Single-platform: file upload or pasted text, same as before.
		platform_commands_map = {}
		commands_file = request.files.get("commands_file")
		manual_commands = request.form.get("manual_commands", "").strip()
		commands = []

		if commands_file and commands_file.filename:
			if not commands_file.filename.lower().endswith(".txt"):
				flash("Command file must be a .txt file.", "danger")
				return redirect(url_for("new_rollout"))
			try:
				for raw_line in commands_file.readlines():
					line = raw_line.decode("utf-8").strip()
					if line:
						commands.append(line)
			except UnicodeDecodeError:
				flash("Command file must be valid UTF-8 text.", "danger")
				return redirect(url_for("new_rollout"))
		else:
			commands = [l.strip() for l in manual_commands.splitlines() if
			            l.strip()]

		if not commands:
			flash(
				"Provide commands by pasting text or uploading a command file.",
				"danger")
			return redirect(url_for("new_rollout"))

	# 5) Collect rollout options from the form.
	verify_flag = request.form.get("_verify", "")
	verbose_flag = request.form.get("_verbose", "")
	options = RolloutOptions(
		verify=bool(verify_flag),
		verbose=bool(verbose_flag),
		webapp=True
	)

	# 6) Load the selected inventory rows belonging to the current user.
	with get_session() as db_session:
		selected_rows = (
			db_session.query(Inventory)
			.filter(
				Inventory.user_id == current_user.id,
				Inventory.id.in_(selected_ids)
			)
			.all()
		)

		# Preload relationships needed for runtime device construction.
		_ = [row.security_profile for row in selected_rows]
		_ = [row.var_mappings for row in selected_rows]

		db_session.expunge_all()

	# 7) Validate the selected devices.
	if not selected_rows:
		flash("No valid devices selected.", "danger")
		return redirect(url_for("new_rollout"))

	if len(selected_rows) != len(selected_ids):
		flash("One or more selected devices were not found.", "danger")
		return redirect(url_for("new_rollout"))

	# 8) Ensure every device has a security profile.
	missing_profiles = [row.label or row.ip for row in selected_rows if
	                    not row.security_profile]
	if missing_profiles:
		flash(
			"These devices have no security profile assigned: "
			+ ", ".join(missing_profiles),
			"danger"
		)
		return redirect(url_for("new_rollout"))

	# 9) Convert ORM inventory rows into runtime Device objects.
	try:
		devices = InputParser.import_from_inventory(selected_rows)
	except ValueError as e:
		flash(str(e), "danger")
		return redirect(url_for("new_rollout"))

	# 10) Submit jobs to the orchestrator.
	audit_comment = request.form.get("comment", "").strip() or None

	if is_multi_platform:
		# Backend enforces multi-platform — never trust the frontend alone.
		actual_platforms = {d.device_type for d in devices}
		if len(actual_platforms) < 2:
			flash("Expected multiple platforms but only one found.", "danger")
			return redirect(url_for("new_rollout"))

		devices.sort(key=lambda d: d.device_type)
		job_id = None
		for platform, group in groupby(devices, key=lambda d: d.device_type):
			curr_commands = [l.strip() for l in
			                 platform_commands_map.get(platform,
			                                           "").splitlines()
			                 if l.strip()]
			if not curr_commands:
				flash(f"No commands provided for {platform}.", "danger")
				return redirect(url_for("new_rollout"))
			first_job = orchestrator.submit(list(group), curr_commands, options,
			                                current_user.id, audit_comment)
			if job_id is None:
				job_id = first_job
	else:
		job_id = orchestrator.submit(devices, commands, options,
		                             current_user.id, audit_comment)

	# 11) Notify the user and redirect to active jobs.
	audit("rollout.start", object_id=job_id,
	      detail={"device_count": len(devices), "comment": audit_comment})
	flash(
		f"Rollout started for {len(devices)} "
		f"device{'s' if len(devices) != 1 else ''}.",
		"success"
	)
	return redirect(url_for("active_jobs", new=str(job_id)))


@app.route("/admin")
@login_required
def admin_panel():
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	return redirect(url_for("admin_users"))


@app.route("/admin/users")
@login_required
def admin_users():
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	with get_session() as db_session:
		users = db_session.query(User).order_by(User.created_at).all()
		db_session.expunge_all()
	return render_template("admin_users.html", users=users,
	                       active_section="users")


@app.route("/admin/users/<uuid:user_id>/<action>", methods=["POST"])
@login_required
def admin_user_action(user_id, action):
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	if action in ("disable", "delete") and user_id == current_user.id:
		flash("You cannot perform this action on your own account.", "danger")
		return redirect(url_for("admin_users"))
	with get_session() as db_session:
		user = db_session.query(User).filter_by(id=user_id).first()

		if not user or user.username == "admin":
			return redirect(url_for("admin_users"))

		target_username = user.username
		target_id = user.id
		if action == "approve":
			user.is_approved = True
			user.is_active = True
		elif action == "enable":
			user.is_active = True
		elif action == "disable":
			user.is_active = False
		elif action == "promote":
			user.role = "admin"
			user.is_approved = True
			user.is_active = True
		elif action == "demote":
			user.role = "user"
		elif action == "delete":
			db_session.delete(user)

	audit(f"user.{action}", object_type="User",
	      object_id=target_id, object_label=target_username)
	return redirect(url_for("admin_users"))


@app.route("/admin/users/bulk/<action>", methods=["POST"])
@login_required
def admin_bulk_action(action):
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	raw = request.form.get("user_ids", "")
	try:
		user_ids = [uuid.UUID(uid.strip()) for uid in raw.split(",") if
		            uid.strip()]
	except ValueError:
		return redirect(url_for("admin_users"))
	affected = 0
	with get_session() as db_session:
		for uid in user_ids:
			user = db_session.get(User, uid)
			if not user or user.username == "admin":
				continue
			if action in ("disable", "delete") and uid == current_user.id:
				continue
			if action == "approve":
				user.is_approved = True
				user.is_active = True
			elif action == "enable":
				user.is_active = True
			elif action == "disable":
				user.is_active = False
			elif action == "promote":
				user.role = "admin"
				user.is_approved = True
				user.is_active = True
			elif action == "demote":
				user.role = "user"
			elif action == "delete":
				db_session.delete(user)
			affected += 1
	audit(f"user.bulk_{action}", detail={"count": affected})
	return redirect(url_for("admin_users"))


@app.route("/admin/server")
@login_required
def admin_server():
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	try:
		with engine.connect() as conn:
			conn.execute(text("SELECT 1"))
		db_connected = True
	except OperationalError:
		db_connected = False
	db_mode = "external" if _CONFIG_ENV.exists() else "local"
	current_config = dotenv_values(_CONFIG_ENV) if _CONFIG_ENV.exists() else {}
	return render_template('server_management.html', active_section="server",
	                       db_mode=db_mode, db_connected=db_connected,
	                       current_config=current_config)


@app.route("/admin/server/db/test", methods=["POST"])
@login_required
def admin_server_db_test():
	if current_user.role != "admin":
		return jsonify(success=False, error="Unauthorized"), 403
	data = request.get_json()
	host, port, name = (data.get("host", "").strip(),
	                    data.get("port", "5432").strip(),
	                    data.get("name", "").strip())
	user, password, schema = (data.get("user", "").strip(),
	                          data.get("password", "").strip(),
	                          data.get("schema", "").strip())
	if not all([host, port, name, user, password]):
		return jsonify(success=False, error="All fields except schema are "
		                                    "required")
	if (host == engine.url.host and
			str(port) == str(engine.url.port) and
			name == engine.url.database):
		return jsonify(success=False, error="Target database is the same as "
		                                    "the current one")
	url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
	connect_args = {"options": f"-c search_path={schema}"} if schema else {}

	try:
		test_engine = create_engine(url, connect_args=connect_args)
		with test_engine.connect() as conn:
			conn.execute(text("SELECT 1"))
		test_engine.dispose()
		return jsonify(success=True)
	except OperationalError as e:
		return jsonify(success=False, error=str(e))


@app.route("/admin/server/db/save", methods=["POST"])
@login_required
def admin_server_db_save():
	if current_user.role != "admin":
		return jsonify(success=False, error="Unauthorized"), 403
	data = request.get_json()
	host, port, name = (data.get("host", "").strip(),
	                    data.get("port", "5432").strip(),
	                    data.get("name", "").strip())
	user, password, schema = (data.get("user", "").strip(),
	                          data.get("password", "").strip(),
	                          data.get("schema", "").strip())
	if not all([host, port, name, user, password]):
		return jsonify(success=False,
		               error="All fields except schema are required")
	if (host == engine.url.host and
			str(port) == str(engine.url.port) and
			name == engine.url.database):
		return jsonify(success=False, error="Target database is the same as "
		                                    "the current one")
	lines = [f"DB_HOST={host}", f"DB_PORT={port}", f"DB_NAME={name}",
	         f"DB_USER={user}", f"DB_PASSWORD={password}"]
	if schema:
		lines.append(f"DB_SCHEMA={schema}")
	_CONFIG_ENV.write_text("\n".join(lines) + "\n")
	_FLAG.write_text(".")
	return jsonify(success=True)


@app.route("/admin/audit")
@login_required
def admin_audit():
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))

	filter_actor = request.args.get("actor", "").strip()
	filter_action = request.args.get("action", "").strip()
	filter_success = request.args.get("success", "")

	with get_session() as db_session:
		q = db_session.query(AuditLog).order_by(AuditLog.timestamp.desc())
		if filter_actor:
			q = q.filter(AuditLog.actor_username.ilike(f"%{filter_actor}%"))
		if filter_action:
			q = q.filter(AuditLog.action == filter_action)
		if filter_success == "true":
			q = q.filter(AuditLog.success == True)
		elif filter_success == "false":
			q = q.filter(AuditLog.success == False)
		entries = q.limit(500).all()
		distinct_actions = [r[0] for r in
		                    db_session.query(AuditLog.action).distinct()
		                    .order_by(AuditLog.action).all()]
		db_session.expunge_all()

	return render_template("admin_audit.html",
	                       entries=entries,
	                       distinct_actions=distinct_actions,
	                       filter_actor=filter_actor,
	                       filter_action=filter_action,
	                       filter_success=filter_success,
	                       active_section="audit")


@app.route("/admin/analytics")
@login_required
def admin_analytics():
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))

	with get_session() as db_session:
		cutoff = datetime.now() - timedelta(days=30)
		results_30d = db_session.query(DeviceResult).filter(
			DeviceResult.started_at >= cutoff
		).all()

		all_users = db_session.query(User).order_by(User.username).all()
		total_users = len(all_users)
		active_user_ids = {r.user_id for r in results_30d}
		total_ops = len(results_30d)
		total_jobs = len({r.job_id for r in results_30d})
		success_count = sum(1 for r in results_30d if r.status == "success")

		org_kpi = {
			"active_users": len(active_user_ids),
			"total_users": total_users,
			"total_jobs": total_jobs,
			"total_ops": total_ops,
			"success_rate": round(
				success_count / total_ops * 100) if total_ops else None,
		}

		user_stats = defaultdict(
			lambda: {"job_ids": set(), "devices": 0, "last_job_at": None})
		for r in results_30d:
			s = user_stats[r.user_id]
			s["job_ids"].add(r.job_id)
			s["devices"] += 1
			if s["last_job_at"] is None or r.completed_at > s["last_job_at"]:
				s["last_job_at"] = r.completed_at

		username_map = {u.id: u.username for u in all_users}
		active_users_rows = sorted(
			[
				{
					"username": username_map.get(uid, str(uid)),
					"job_count": len(s["job_ids"]),
					"devices_reached": s["devices"],
					"last_job_at": s["last_job_at"],
				}
				for uid, s in user_stats.items()
			],
			key=lambda x: x["job_count"],
			reverse=True,
		)[:10]

		fail_counts = defaultdict(lambda: {"device_type": "", "count": 0})
		for r in results_30d:
			if r.status == "failed":
				fail_counts[r.device_ip]["device_type"] = r.device_type
				fail_counts[r.device_ip]["count"] += 1

		failed_ips = set(fail_counts.keys())
		inv_rows = (
			db_session.query(Inventory.ip, Inventory.label)
			.filter(Inventory.ip.in_(failed_ips))
			.all()
			if failed_ips else []
		)
		label_map = {row.ip: row.label for row in inv_rows}

		failed_devices_rows = sorted(
			[
				{
					"ip": ip,
					"label": label_map.get(ip),
					"device_type": s["device_type"],
					"fail_count": s["count"],
				}
				for ip, s in fail_counts.items()
			],
			key=lambda x: x["fail_count"],
			reverse=True,
		)[:10]

		db_session.expunge_all()

	return render_template(
		"admin_analytics.html",
		org_kpi=org_kpi,
		active_users=active_users_rows,
		all_users=[{"id": str(u.id), "username": u.username} for u in
		           all_users],
		failed_devices=failed_devices_rows,
		active_section="analytics",
	)


@app.route("/admin/analytics/query", methods=["POST"])
@login_required
def admin_analytics_query():
	if current_user.role != "admin":
		return jsonify({"error": "Forbidden"}), 403

	data = request.get_json()
	try:
		rules = data.get("rules", [])
		filters = compile_query_rules(rules, QUERY_AUDIT_LOG_FIELDS)
	except (ValueError, KeyError) as e:
		return jsonify({"error": str(e)}), 400

	with get_session() as db_session:
		query = db_session.query(AuditLog).filter(filters)

		rows_raw = query.order_by(AuditLog.timestamp.desc()).limit(
			200).all()
		columns = AUDIT_LOG_COLUMNS
		rows = [{col: getattr(r, col) for col in columns} for r in rows_raw]
	parsed_rows = [{col: v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v,
		                                                                   datetime)
		else str(v) if isinstance(v, uuid.UUID)
		else v
		                for col, v in row.items()}
		               for row in rows]
	return jsonify({"columns": columns, "rows": parsed_rows})


@app.route("/analytics")
@login_required
def analytics():
	selected_user = "me"
	scope_user_id = current_user.id

	if current_user.role == "admin":
		param = request.args.get("user", "me").strip()
		if param != "me":
			try:
				scope_user_id = uuid.UUID(param)
				selected_user = param
			except ValueError:
				pass

	with get_session() as db_session:
		cutoff = datetime.now() - timedelta(days=30)
		results_30d = db_session.query(DeviceResult).filter(
			DeviceResult.started_at >= cutoff,
			DeviceResult.user_id == scope_user_id,
		).all()

		inv_label_map = {
			row.ip: row.label
			for row in db_session.query(Inventory.ip, Inventory.label)
			.filter(Inventory.user_id == scope_user_id).all()
		}
		users = db_session.query(User).order_by(User.username).all() \
			if current_user.role == "admin" else []
		db_session.expunge_all()

	total_ops = len(results_30d)
	jobs_30d = len({r.job_id for r in results_30d})
	success_count = sum(1 for r in results_30d if r.status == "success")

	fail_counts_ip: dict[str, int] = defaultdict(int)
	for r in results_30d:
		if r.status == "failed":
			fail_counts_ip[r.device_ip] += 1
	top_failed = None
	if fail_counts_ip:
		top_ip = max(fail_counts_ip, key=lambda ip: fail_counts_ip[ip])
		top_failed = {"ip": top_ip, "label": inv_label_map.get(top_ip),
		              "fail_count": fail_counts_ip[top_ip]}

	platform_counts = Counter(r.device_type for r in results_30d)
	top_platforms = platform_counts.most_common(3)  # [(name, count), ...]

	kpi = {
		"success_rate": round(
			success_count / total_ops * 100) if total_ops else None,
		"jobs_30d": jobs_30d,
		"devices_reached": total_ops,
		"commands_pushed": sum(r.commands_sent for r in results_30d),
		"top_failed": top_failed,
		"top_platforms": top_platforms,
	}

	selected_username = next(
		(u.username for u in users if str(u.id) == selected_user), selected_user
	) if selected_user != "me" else "me"

	return render_template("analytics.html",
	                       kpi=kpi,
	                       users=users,
	                       selected_user=selected_user,
	                       selected_username=selected_username,
	                       active_section="analytics")


def compile_query_rules(node, allowed_fields):
	'''jQuery QueryBuilder produces a tree. Each node is either:
	 - a GROUP: {"condition": "AND"/"OR", "rules": [...child
	nodes...]}
	 - a LEAF:  {"field": "status", "operator": "equal", "value":
	"success"}'''

	if "condition" in node:
		# GROUP node — recurse into each child, then combine with
		# AND / OR
		combinator = and_ if node["condition"] == "AND" else or_
		return combinator(*[compile_query_rules(r, allowed_fields) for r in
		                    node["rules"]])
	# LEAF node — a single filter condition
	field_name = node["field"]
	operator = node["operator"]
	value = node["value"]

	# Security(SQL injection hardening): reject fields/operators
	# not in our allowlist
	if field_name not in allowed_fields:
		raise ValueError(f"Field not allowed: {field_name}")

	column, allowed_ops = allowed_fields[field_name]
	if operator not in allowed_ops:
		raise ValueError(
			f"Operator {operator} not allowed for field {field_name}")

	# DateTime columns need a Python datetime object, not a raw string
	if hasattr(column, "type") and column.type.__class__.__name__ == 'DateTime':
		try:
			value = datetime.strptime(value, "%Y-%m-%d")
		except (ValueError,TypeError):
			raise ValueError(f"Invalid date: {value}")

	# Boolean columns: QueryBuilder sends string keys ("true"/"false")
	if hasattr(column, "type") and column.type.__class__.__name__ == 'Boolean':
		if isinstance(value, str):
			value = value.lower() == "true"

	# Dispatch to the right SQLAlchemy expression via the OPS table
	return QUERY_OPS[operator](column, value)


@app.route("/analytics/query", methods=["POST"])
@login_required
def analytics_query():
	scope_user_id = current_user.id
	data = request.get_json()
	if current_user.role == "admin":
		param = data.get("user", "me").strip()
		if param != "me":
			try:
				scope_user_id = uuid.UUID(param)
			except ValueError:
				pass
	try:
		rules = data.get("rules", [])
		filters = compile_query_rules(rules, QUERY_DEVICE_RESULT_FIELDS)
	except (ValueError, KeyError) as e:
		return jsonify({"error": str(e)}), 400

	with get_session() as db_session:
		query = db_session.query(DeviceResult).filter(
			DeviceResult.user_id == scope_user_id).filter(filters)

		rows_raw = query.order_by(DeviceResult.started_at.desc()).limit(
			200).all()
		columns = DEVICE_RESULT_COLUMNS
		rows = [{col: getattr(r, col) for col in columns} for r in rows_raw]
	parsed_rows = [{col: v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v,
		                                                                   datetime)
		else str(v) if isinstance(v, uuid.UUID)
		else v
		                for col, v in row.items()}
		               for row in rows]
	return jsonify({"columns": columns, "rows": parsed_rows})


def job_status(rows: list[DeviceResult]) -> str:
	statuses = {r.status for r in rows}
	if "cancelled" in statuses:
		return "cancelled"
	if all(r.status == "failed" for r in rows):
		return "failed"
	if any(r.status in ("failed", "partial") for r in rows):
		return "partial"
	return "success"


@app.route("/dashboard")
@login_required
def dashboard():
	# ── Admin KPI scope ───────────────────────────────────────────────────────
	selected_user = "me"
	kpi_user_id = current_user.id
	if current_user.role == "admin":
		param = request.args.get("user", "me").strip()
		if param != "me":
			try:
				kpi_user_id = uuid.UUID(param)
				selected_user = param
			except ValueError:
				pass

	with get_session() as db_session:
		# Current user's dashboard content (always own data)
		user = db_session.get(User, current_user.id)
		inventory_count = len(user.inventory)
		profile_count = len(user.security_profiles)
		mapping_count = len(user.variable_mappings)
		jobs_results = user.results  # DeviceResult rows
		inv_label_map = {d.ip: d.label for d in user.inventory}

		# KPI data — scoped to kpi_user_id (may differ from current user for admin)
		cutoff = datetime.now() - timedelta(days=30)
		if kpi_user_id == current_user.id:
			kpi_results_30d = [r for r in jobs_results if
			                   r.started_at >= cutoff]
			kpi_label_map = inv_label_map
		else:
			kpi_results_30d = db_session.query(DeviceResult).filter(
				DeviceResult.user_id == kpi_user_id,
				DeviceResult.started_at >= cutoff,
			).all()
			kpi_label_map = {
				row.ip: row.label
				for row in db_session.query(Inventory.ip, Inventory.label)
				.filter(Inventory.user_id == kpi_user_id).all()
			}

		users = db_session.query(User).order_by(User.username).all() \
			if current_user.role == "admin" else []
		db_session.expunge_all()

	sorted_results = sorted(jobs_results, key=lambda x: x.job_id)
	job_summaries = []
	for job_id, rows in groupby(sorted_results, key=lambda x: x.job_id):
		rows = list(rows)
		job_summaries.append({
			"job_id": job_id,
			"completed_at": max(r.completed_at for r in rows),
			"device_count": len(rows),
			"commands_sent": rows[0].commands_sent,
			"status": job_status(rows),
		})

	job_summaries.sort(key=lambda x: x["completed_at"], reverse=True)
	recent_jobs = job_summaries[:5]
	total_rollouts = len(job_summaries)
	last_status = job_summaries[0]["status"] if job_summaries else None

	# ── 30-day KPI strip (scoped) ─────────────────────────────────────────────
	total_ops = len(kpi_results_30d)
	jobs_30d = len({r.job_id for r in kpi_results_30d})
	success_count = sum(1 for r in kpi_results_30d if r.status == "success")
	fail_counts: dict[str, int] = defaultdict(int)
	for r in kpi_results_30d:
		if r.status == "failed":
			fail_counts[r.device_ip] += 1
	top_failed = None
	if fail_counts:
		top_ip = max(fail_counts, key=lambda ip: fail_counts[ip])
		top_failed = {"ip": top_ip, "label": kpi_label_map.get(top_ip),
		              "fail_count": fail_counts[top_ip]}
	kpi = {
		"success_rate": round(
			success_count / total_ops * 100) if total_ops else None,
		"jobs_30d": jobs_30d,
		"devices_reached": total_ops,
		"commands_pushed": sum(r.commands_sent for r in kpi_results_30d),
		"top_failed": top_failed,
	}

	# ── Active job (always own) ───────────────────────────────────────────────
	job_id = session.get("job_id", None)
	active_job = orchestrator.get(uuid.UUID(job_id)) if job_id else None

	active_job_data = None
	if active_job and active_job.is_alive():
		active_job_data = {
			"job_id": job_id,
			"device_count": active_job.get_device_count(),
			"started_at": active_job.started_at.strftime("%H:%M:%S"),
			"started_at_iso": active_job.started_at.isoformat(),
		}

	selected_username = next(
		(u.username for u in users if str(u.id) == selected_user), selected_user
	) if selected_user != "me" else "me"

	return render_template("dashboard.html",
	                       active_section="dashboard",
	                       active_job=active_job_data,
	                       recent_jobs=recent_jobs,
	                       inventory_count=inventory_count,
	                       profile_count=profile_count,
	                       mapping_count=mapping_count,
	                       total_rollouts=total_rollouts,
	                       last_status=last_status,
	                       kpi=kpi,
	                       users=users,
	                       selected_user=selected_user,
	                       selected_username=selected_username)


@app.route("/inventory")
@login_required
def inventory():
	sys_props, user_props = get_property_defs(current_user.id)
	with get_session() as db_session:
		user = db_session.get(User, current_user.id)
		devices = user.inventory
		profiles = user.security_profiles
		var_mappings = user.variable_mappings
		_ = [d.security_profile for d in devices]
		_ = [d.var_mappings for d in devices]
		db_session.expunge_all()
	return render_template("inventory.html",
	                       devices=devices,
	                       profiles=profiles,
	                       mappings=var_mappings,
	                       sys_props=sys_props,
	                       user_props=user_props,
	                       active_section="inventory")


@app.route("/inventory/create", methods=["POST"])
@login_required
def inventory_create():
	label = request.form.get("label", "").strip()
	ip = request.form.get("ip", "").strip()
	port = request.form.get("port", "22").strip()
	device_type = request.form.get("device_type", "").strip()
	sec_profile_id = request.form.get("sec_profile_id", "").strip()

	with get_session() as db_session:
		row = Inventory(
			user_id=current_user.id,
			label=label,
			ip=ip,
			port=int(port),
			device_type=device_type,
			sec_profile_id=uuid.UUID(sec_profile_id) if sec_profile_id else None
		)
		db_session.add(row)

	audit("inventory.create", object_type="Inventory", object_label=label)
	flash(f"{label} added to inventory.", "success")
	return redirect(url_for("inventory"))


@app.route("/inventory/test_connection", methods=["POST"])
@login_required
def inventory_test_connection():
	data = request.get_json()
	if not data:
		return {"status": "error", "message": "Invalid request"}

	ip = str(data.get("ip", "")).strip()
	port = str(data.get("port", "")).strip()

	if not Validator.validate_ip(ip):
		return {"status": "error", "message": "Invalid IP address"}
	if not Validator.validate_port(port):
		return {"status": "error",
		        "message": "Port must be between 1 and 65535"}

	if Validator.test_tcp_port(ip, int(port)):
		return {"status": "success",
		        "message": f"TCP port {port} reachable on {ip}"}
	return {"status": "error",
	        "message": f"TCP port {port} unreachable on {ip}"}


@app.route("/inventory/<uuid:device_id>/edit", methods=["POST"])
@login_required
def inventory_edit(device_id):
	with get_session() as db_session:
		device = db_session.query(Inventory).filter_by(
			id=device_id, user_id=current_user.id).first()
		if not device:
			flash("Device not found.", "danger")
			return redirect(url_for("inventory"))

		device.label = request.form.get("label", "").strip()
		device.ip = request.form.get("ip", "").strip()
		device.port = int(request.form.get("port", 22))
		device.device_type = request.form.get("device_type", "").strip()

		sec_profile_id = request.form.get("sec_profile_id", "").strip()
		device.sec_profile_id = uuid.UUID(
			sec_profile_id) if sec_profile_id else None

		sys_props, user_props = get_property_defs(current_user.id)
		all_props = {p["name"]: p for p in sys_props + user_props}
		var_maps = {}
		for key, val in request.form.items():
			if not key.startswith("attr_"):
				continue
			prop_name = key[5:]
			val = val.strip()
			if not val:
				continue
			if all_props.get(prop_name, {}).get("is_list"):
				var_maps[prop_name] = [v.strip() for v in val.split(",") if
				                       v.strip()]
			else:
				var_maps[prop_name] = val
		device.var_maps = var_maps or None

		mapping_ids = request.form.getlist("mapping_ids")
		if mapping_ids:
			selected = db_session.query(VariableMapping).filter(
				VariableMapping.id.in_([uuid.UUID(mid) for mid in mapping_ids]),
				VariableMapping.user_id == current_user.id
			).all()
			device.var_mappings = selected
		else:
			device.var_mappings = []

		label = device.label

	audit("inventory.edit", object_type="Inventory",
	      object_id=device_id, object_label=label)
	flash(f"{label} updated.", "success")
	return redirect(url_for("inventory"))


@app.route("/inventory/<uuid:device_id>/delete", methods=["POST"])
@login_required
def inventory_delete(device_id):
	with get_session() as db_session:
		device = db_session.query(Inventory).filter_by(
			id=device_id, user_id=current_user.id).first()
		if not device:
			flash("Device not found.", "danger")
			return redirect(url_for("inventory"))
		label = device.label
		db_session.delete(device)

	audit("inventory.delete", object_type="Inventory",
	      object_id=device_id, object_label=label)
	flash(f"{label} removed from inventory.", "success")
	return redirect(url_for("inventory"))


@app.route("/inventory/import_csv", methods=["POST"])
@login_required
def inventory_import_csv():
	"""
	Bulk-imports devices from an uploaded CSV file into the user's inventory.
	Saves the upload to a temp file, delegates to InputParser.csv_to_inventory,
	which validates each row and TCP-checks each device before writing.
	NOTE: TCP checks are sequential — large CSVs will block the web process.
		  Phase 3.6 per-device concurrency will address this.
	Error messages from the parser are captured by draining the logger queue
	after the call and flashed to the user.
	"""
	csv_file = request.files.get("csv_file")
	if not csv_file or not csv_file.filename:
		flash("No file selected.", "danger")
		return redirect(url_for("inventory"))

	label = request.form.get("label", "").strip() or None

	# Save upload to a temp file — csv_to_inventory takes a path, not a file object
	with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
		tmp_path = tmp.name
		csv_file.save(tmp_path)

	# Temp logfile — RolloutLogger always writes to disk, we don't need it here.
	# Phase 3 will add a proper activity logging workflow.
	log_path = tempfile.mktemp(suffix=".log")

	try:
		# TODO verify correctness
		logger = RolloutLogger(webapp=True, verbose=False)
		validator = Validator(logger)
		parser = InputParser(validator, logger)

		with get_session() as db_session:
			devices = parser.csv_to_inventory(
				tmp_path, current_user.id, db_session, label=label)

		# Drain error messages queued by the parser (red-colored notify calls)
		errors = []
		while True:
			try:

				errors.append(logger.get_queue(0))
			except Empty:
				break

		if errors:
			for msg in errors:
				flash(msg, "danger")
		if devices:
			audit("inventory.import_csv", detail={"count": len(devices)})
			flash(
				f"{len(devices)} device{'s' if len(devices) != 1 else ''} imported successfully.",
				"success")
		elif not errors:
			flash("No valid devices found in CSV.", "warning")

	finally:
		os.unlink(tmp_path)
		if os.path.exists(log_path):
			os.unlink(log_path)

	return redirect(url_for("inventory"))


@app.route("/inventory/bulk_assign", methods=["POST"])
@login_required
def inventory_bulk_assign():
	data = request.get_json(silent=True)
	if not data:
		return jsonify({"status": "error", "message": "Invalid request"}), 400

	profile_id = data.get("profile_id")
	device_ids = data.get("device_ids", [])

	if not device_ids:
		return jsonify(
			{"status": "error", "message": "No devices provided"}), 400

	parsed_profile_id = uuid.UUID(profile_id) if profile_id else None

	with get_session() as db_session:
		if parsed_profile_id:
			profile = db_session.query(SecurityProfile).filter_by(
				id=parsed_profile_id, user_id=current_user.id).first()
			if not profile:
				return jsonify(
					{"status": "error", "message": "Profile not found"}), 404

		for device_id_str in device_ids:
			device = db_session.query(Inventory).filter_by(
				id=uuid.UUID(device_id_str), user_id=current_user.id).first()
			if device:
				device.sec_profile_id = parsed_profile_id

	audit("inventory.bulk_assign", detail={
		"count": len(device_ids),
		"profile_id": str(profile_id) if profile_id else None})
	return jsonify({"status": "success"})


@app.route("/active_jobs")
@login_required
def active_jobs():
	is_admin = current_user.role == "admin"
	with get_session() as db_session:
		if is_admin:
			sessions = db_session.query(RolloutSession).all()
			usernames = {u.id: u.username for u in db_session.query(User).all()}
		else:
			sessions = db_session.query(RolloutSession).filter_by(
				user_id=current_user.id).all()
			usernames = {}
		db_session.expunge_all()

	def _build_job_dict(s):
		job = orchestrator.get(s.id)
		return {
			"id": str(s.id),
			"status": s.status,
			"created_at": s.created_at,
			"device_count": job.get_device_count() if job else "—",
			"started_at": job.started_at.strftime(
				"%H:%M:%S") if job and job.started_at else "—",
			"started_at_iso": job.started_at.isoformat() if job and job.started_at else "",
			"owner": usernames.get(s.user_id, "unknown"),
		}

	if is_admin:
		jobs = [_build_job_dict(s) for s in sessions if
		        s.user_id == current_user.id]
		other_jobs = [_build_job_dict(s) for s in sessions if
		              s.user_id != current_user.id]
	else:
		jobs = [_build_job_dict(s) for s in sessions]
		other_jobs = []

	new_job_id = request.args.get("new", "")
	return render_template("active_jobs.html", jobs=jobs, other_jobs=other_jobs,
	                       is_admin=is_admin,
	                       new_job_id=new_job_id, active_section="active_jobs")


@app.route("/rollout_stream/<uuid:job_id>")
@login_required
def rollout_stream(job_id):
	job = orchestrator.get(job_id)
	if not job or (
			job.user_id != current_user.id and current_user.role != "admin"):
		return Response(status=403)

	def generate():
		snapshot = job.get_log_history()
		for msg in snapshot:
			yield f"data: {msg}\n\n"

		# Drain queue items already covered by the buffer snapshot
		for _ in snapshot:
			try:
				job.get_log_queue()
			except Empty:
				break

		while job.is_alive():
			try:
				msg = job.get_log_queue()
				yield f"data: {msg}\n\n"
			except Empty:
				yield "data: \n\n"
				sleep(0.5)

		# Drain any messages queued in the final moments before is_alive() went False
		while True:
			try:
				msg = job.get_log_queue()
				yield f"data: {msg}\n\n"
			except Empty:
				break

		yield "event: done\ndata: \n\n"

	return Response(
		generate(),
		mimetype="text/event-stream",
		headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
	)


@app.route("/rollback/<uuid:job_id>", methods=["POST"])
@login_required
def rollback(job_id):
	# Get successful devices
	with get_session() as db_session:
		result = db_session.query(DeviceResult).filter_by(
			user_id=current_user.id,
			job_id=job_id,
			status="success").all()
		successful_ips = {r.device_ip for r in result}

		rows = db_session.query(Inventory).filter(
			Inventory.user_id == current_user.id,
			Inventory.ip.in_(successful_ips)).all()
		if not rows:
			return jsonify({"status": "error",
			                "message": "No successfully configured"
			                           " devices found for this job."}), 400

		_ = [row.security_profile for row in rows]
		_ = [row.var_mappings for row in rows]
		db_session.expunge_all()

	# Fetch compensatory commands
	data = request.get_json(silent=True)
	if not data or not data.get("commands", "").strip():
		return jsonify(
			{"status": "error", "message": "No commands provided"}), 400

	commands = [l.strip() for l in data["commands"].splitlines() if l.strip()]
	devices = input_parser.InputParser.import_from_inventory(rows)
	options = RolloutOptions(
		verify=bool(data.get("verify", False)),
		verbose=bool(data.get("verbose", False)),
		webapp=True
	)
	new_job_id = orchestrator.submit(devices, commands, options,
	                                 current_user.id)
	audit("rollout.rollback", object_id=job_id,
	      detail={"new_job_id": str(new_job_id), "device_count": len(devices)})
	return jsonify({"status": "ok", "job_id": str(new_job_id)})


@app.route("/results")
@login_required
def results():
	is_admin = current_user.role == "admin"
	with get_session() as db_session:
		if is_admin:
			raw_results = db_session.query(DeviceResult).all()
			metadata_rows = db_session.query(JobMetadata).all()
			usernames = {u.id: u.username for u in db_session.query(User).all()}
			inv_rows = db_session.query(Inventory).all()
		else:
			user = db_session.get(User, current_user.id)
			raw_results = user.results
			metadata_rows = user.job_metadata
			usernames = {}
			inv_rows = user.inventory
		ip_to_label = {row.ip: (row.label or row.ip) for row in inv_rows}
		db_session.expunge_all()

	metadata_by_job = {m.job_id: m for m in metadata_rows}

	def _build_jobs(result_rows, job_owner=None):
		sorted_rows = sorted(result_rows, key=lambda x: x.job_id)
		out = []
		for job_id, rows in groupby(sorted_rows, key=lambda x: x.job_id):
			rows = list(rows)
			meta = metadata_by_job.get(job_id)
			log_matches = glob.glob(
				os.path.join(LOGS_DIR, f"rollout_*_{job_id}.log"))
			entry = {
				"job_id": str(job_id),
				"has_log": bool(log_matches),
				"started_at": min(r.started_at for r in rows),
				"completed_at": max(r.completed_at for r in rows),
				"device_count": len(rows),
				"commands_sent": rows[0].commands_sent,
				"status": job_status(rows),
				"comment": meta.comment if meta else None,
				"commands": meta.commands if meta else [],
				"devices": [
					{
						"ip": r.device_ip,
						"label": ip_to_label.get(r.device_ip, r.device_ip),
						"device_type": r.device_type,
						"status": r.status,
						"commands_sent": r.commands_sent,
						"commands_verified": r.commands_verified,
						"fetched_config": r.fetched_config
					}
					for r in rows
				]
			}
			if job_owner is not None:
				entry["job_owner"] = job_owner
			out.append(entry)
		out.sort(key=lambda x: x["completed_at"], reverse=True)

		return out

	if is_admin:
		my_raw = [r for r in raw_results if r.user_id == current_user.id]
		other_raw = [r for r in raw_results if r.user_id != current_user.id]
		jobs = _build_jobs(my_raw)
		other_jobs = []
		# group other_raw by user_id so each job gets its job_owner username
		other_raw_sorted = sorted(other_raw, key=lambda x: x.user_id)
		for user_id, user_rows in groupby(other_raw_sorted,
		                                  key=lambda x: x.user_id):
			owner = usernames.get(user_id, "unknown")
			other_jobs.extend(_build_jobs(list(user_rows), job_owner=owner))
		other_jobs.sort(key=lambda x: x["completed_at"], reverse=True)
	else:
		jobs = _build_jobs(raw_results)
		other_jobs = []

	return render_template("results.html",
	                       active_section="results",
	                       jobs=jobs,
	                       other_jobs=other_jobs,
	                       is_admin=is_admin)


@app.route("/results/config_diff/<uuid:job_id>/<device_ip>")
@login_required
def config_diff(job_id, device_ip):
	with get_session() as db_session:
		row = db_session.query(DeviceResult).filter_by(
			job_id=job_id, device_ip=device_ip).first()
		if not row:
			return jsonify({"status": "error", "message": "Not found"}), 404
		if current_user.role != "admin" and row.user_id != current_user.id:
			return jsonify({"status": "error", "message": "Forbidden"}), 403
		config = row.fetched_config
		meta = db_session.query(JobMetadata).filter_by(job_id=job_id).first()
		commands = meta.commands if meta else []
	return jsonify({"config": config, "commands": commands})


@app.route("/results/download_log/<uuid:job_id>")
@login_required
def download_log(job_id):
	if current_user.role != "admin":
		with get_session() as db_session:
			owned = db_session.query(DeviceResult).filter_by(
				job_id=job_id, user_id=current_user.id).first()
		if not owned:
			return Response("Not found", status=404)
	matches = glob.glob(os.path.join(LOGS_DIR, f"rollout_*_{job_id}.log"))
	if not matches:
		return Response("Log file not found", status=404)
	return send_file(matches[0], as_attachment=True,
	                 download_name=os.path.basename(matches[0]))


@app.route("/security")
@login_required
def security():
	with get_session() as db_session:
		user = db_session.get(User, current_user.id)
		profiles = user.security_profiles
		_ = [p.inventory for p in profiles]
		devices = user.inventory
		db_session.expunge_all()

	return render_template("security.html",
	                       profiles=profiles,
	                       devices=devices,
	                       active_section="security")


@app.route("/security/create", methods=["POST"])
@login_required
def security_create():
	label = request.form.get("label", "").strip() or None
	username = request.form["username"]
	password = request.form["password"]
	enable_secret = request.form.get("enable_secret", "").strip() or None

	profile = SecurityProfile(label=label,
	                          username=username,
	                          password_secret=encryption.encrypt(password),
	                          enable_secret=encryption.encrypt(enable_secret)
	                          if enable_secret else None,
	                          user_id=current_user.id)

	with get_session() as db_session:
		db_session.add(profile)
	audit("security_profile.create", object_type="SecurityProfile",
	      object_label=label or username)
	flash("Security profile created.", "success")
	return redirect(url_for("security"))


@app.route("/security/quick_create", methods=["POST"])
@login_required
def security_quick_create():
	data = request.get_json()
	if not data:
		return jsonify({"status": "error", "message": "Invalid request"})
	label = str(data.get("label", "") or "").strip() or None
	username = str(data.get("username", "") or "").strip()
	password = str(data.get("password", "") or "")
	enable_secret = str(data.get("enable_secret", "") or "").strip() or None
	if not username or not password:
		return jsonify({"status": "error",
		                "message": "Username and password are required"})
	profile = SecurityProfile(label=label, username=username,
	                          password_secret=encryption.encrypt(password),
	                          enable_secret=encryption.encrypt(enable_secret)
	                          if enable_secret else None,
	                          user_id=current_user.id)
	with get_session() as db_session:
		db_session.add(profile)
		db_session.flush()
		profile_id = str(profile.id)
	audit("security_profile.create", object_type="SecurityProfile",
	      object_label=label or username)
	return jsonify(
		{"status": "ok", "id": profile_id, "label": label or username})


@app.route("/security/<uuid:profile_id>/edit", methods=["POST"])
@login_required
def security_edit(profile_id):
	with get_session() as db_session:
		profile = db_session.query(SecurityProfile).filter_by(
			id=profile_id, user_id=current_user.id).first()
		if not profile:
			return redirect(url_for("security"))

		profile.label = request.form.get("label", "").strip() or None
		profile.username = request.form["username"]

		new_password = request.form.get("password", "").strip()
		if new_password:
			profile.password_secret = encryption.encrypt(new_password)

		new_secret = request.form.get("enable_secret", "").strip()
		if new_secret:
			profile.enable_secret = encryption.encrypt(new_secret)
		elif request.form.get("clear_enable_secret"):
			profile.enable_secret = None

	audit("security_profile.edit", object_type="SecurityProfile",
	      object_id=profile_id)
	flash("Security profile updated.", "success")
	return redirect(url_for("security"))


@app.route("/security/<uuid:profile_id>/delete", methods=["POST"])
@login_required
def security_delete(profile_id):
	with get_session() as db_session:
		profile = db_session.query(SecurityProfile).filter_by(
			id=profile_id, user_id=current_user.id).first()
		if not profile:
			return redirect(url_for("security"))

		if profile.inventory:
			flash(f"Cannot delete '{profile.label or profile.username}' — "
			      f"{len(profile.inventory)} device(s) assigned. "
			      f"Delete or reassign them first.", "danger")
			return redirect(url_for("security"))
		label_or_user = profile.label or profile.username
		db_session.delete(profile)
		flash("Profile deleted.", "success")
	audit("security_profile.delete", object_type="SecurityProfile",
	      object_id=profile_id, object_label=label_or_user)
	return redirect(url_for("security"))


@app.route("/security/<uuid:profile_id>/test", methods=["POST"])
@login_required
def security_test(profile_id):
	data = request.get_json()
	if not data or not data.get("device_id"):
		return {"status": "error", "message": "No device selected"}

	try:
		device_id = uuid.UUID(data["device_id"])
	except ValueError:
		return {"status": "error", "message": "Invalid request"}

	with get_session() as db_session:
		profile = db_session.query(SecurityProfile).filter_by(
			id=profile_id, user_id=current_user.id).first()
		device = db_session.query(Inventory).filter_by(
			id=device_id, user_id=current_user.id).first()
		if not profile or not device:
			return {"status": "error", "message": "Profile or device not found"}
		if not Validator.test_tcp_port(device.ip, device.port):
			return {"status": "error", "message":
				f"TCP port {device.port} unreachable on {device.ip}"}
		db_session.expunge_all()

	device_obj = Device(ip=device.ip,
	                    port=device.port,
	                    device_type=device.device_type,
	                    label=device.label,
	                    username=profile.username,
	                    password=encryption.decrypt(profile.password_secret),
	                    secret=encryption.decrypt(profile.enable_secret)
	                    if profile.enable_secret else ""
	                    )
	try:
		conn = ConnectHandler(**device_obj.netmiko_connector())
		conn.disconnect()
		return {"status": "success",
		        "message": f"Connected successfully to {device.ip}"}
	except NetmikoAuthenticationException:
		return {"status": "error",
		        "message": "Authentication failed — check username and password"}
	except NetmikoTimeoutException:
		return {"status": "error",
		        "message": f"Connection timed out on {device.ip}:{device.port}"}
	except Exception as e:
		return {"status": "error", "message": str(e)}


@app.route("/mappings")
@login_required
def mappings():
	with get_session() as db_session:
		user = db_session.get(User, current_user.id)
		var_binds = user.variable_mappings
		_ = [m.devices for m in var_binds]
		devices = user.inventory
		db_session.expunge_all()

	sys_props, user_props = get_property_defs(current_user.id)
	return render_template("variable_mappings.html", mappings=var_binds,
	                       devices=devices, sys_props=sys_props,
	                       user_props=user_props, active_section="mappings")


@app.route("/mappings/create", methods=["POST"])
@login_required
def mappings_create():
	label = request.form.get("label", "").strip() or None
	inner_token = request.form["token_inner"].strip().upper()
	property_name = request.form["property_name"]
	index = int(request.form.get("index", "").strip()) if request.form.get(
		"index", "").strip() else None

	status, msg = Validator.validate_var_map_inner_token(inner_token)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings"))

	status, msg = Validator.validate_var_map_property_name(property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings"))

	status, msg = Validator.validate_var_index(index, property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings"))

	token = f"$${inner_token}$$"
	row = VariableMapping(
		label=label,
		token=token,
		index=index,
		property_name=property_name,
		user_id=current_user.id
	)

	try:
		with get_session() as db_session:
			db_session.add(row)
		audit("mapping.create", object_type="VariableMapping",
		      object_label=token)
		flash("Mapping created.", "success")
	except IntegrityError:
		audit("mapping.create", success=False,
		      detail={"reason": "duplicate_token", "token": token})
		flash("A mapping with that token already exists.", "danger")

	return redirect(url_for("mappings"))


@app.route("/mappings/quick_create", methods=["POST"])
@login_required
def mappings_quick_create():
	data = request.get_json()
	if not data:
		return jsonify({"status": "error", "message": "Invalid request"})
	inner_token = str(data.get("token_inner", "") or "").strip().upper()
	property_name = str(data.get("property_name", "") or "").strip()
	index_raw = data.get("index")
	index = int(index_raw) if index_raw is not None else None

	status, msg = Validator.validate_var_map_inner_token(inner_token)
	if not status:
		return jsonify({"status": "error", "message": msg})
	status, msg = Validator.validate_var_map_property_name(property_name)
	if not status:
		return jsonify({"status": "error", "message": msg})
	status, msg = Validator.validate_var_index(index, property_name)
	if not status:
		return jsonify({"status": "error", "message": msg})

	token = f"$${inner_token}$$"
	row = VariableMapping(token=token, index=index, property_name=property_name,
	                      user_id=current_user.id)
	try:
		with get_session() as db_session:
			db_session.add(row)
			db_session.flush()
			mapping_id = str(row.id)
	except IntegrityError:
		audit("mapping.create", success=False,
		      detail={"reason": "duplicate_token", "token": token})
		return jsonify(
			{"status": "error", "message": f"Token {token} already exists"})
	audit("mapping.create", object_type="VariableMapping", object_label=token)
	return jsonify({"status": "ok", "id": mapping_id, "token": token,
	                "property_name": property_name, "index": index})


@app.route("/mappings/<uuid:mapping_id>/edit", methods=["POST"])
@login_required
def mappings_edit(mapping_id):
	label = request.form.get("label", "").strip() or None
	inner_token = request.form["token_inner"].strip().upper()
	property_name = request.form["property_name"]
	index = int(request.form.get("index", "").strip()) if request.form.get(
		"index", "").strip() else None

	status, msg = Validator.validate_var_map_inner_token(inner_token)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings"))

	status, msg = Validator.validate_var_map_property_name(property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings"))

	status, msg = Validator.validate_var_index(index, property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings"))

	token = f"$${inner_token}$$"

	try:
		with get_session() as db_session:
			mapping = db_session.query(VariableMapping).filter_by(
				id=mapping_id, user_id=current_user.id
			).first()

			if not mapping:
				flash("Mapping not found.", "danger")
				return redirect(url_for("mappings"))

			mapping.label = label
			mapping.token = token
			mapping.property_name = property_name
			mapping.index = index

		audit("mapping.edit", object_type="VariableMapping",
		      object_id=mapping_id, object_label=token)
		flash("Mapping updated.", "success")
	except IntegrityError:
		audit("mapping.edit", success=False,
		      detail={"reason": "duplicate_token", "token": token})
		flash("A mapping with that token already exists.", "danger")

	return redirect(url_for("mappings"))


@app.route("/mappings/<uuid:mapping_id>/delete", methods=["POST"])
@login_required
def mappings_delete(mapping_id):
	with get_session() as db_session:
		mapping = db_session.query(VariableMapping).filter_by(
			id=mapping_id, user_id=current_user.id).first()
		if not mapping:
			return redirect(url_for("mappings"))

		token_label = mapping.token
		db_session.delete(mapping)
		flash("Mapping deleted.", "success")
	audit("mapping.delete", object_type="VariableMapping",
	      object_id=mapping_id, object_label=token_label)
	return redirect(url_for("mappings"))


@app.route("/mappings/bulk_assign", methods=["POST"])
@login_required
def mappings_bulk_assign():
	"""
	Assigns a list of inventory devices to a variable mapping via the
	many-to-many join table. Accepts a JSON body with mapping_id and
	device_ids. For each device, three checks are enforced before appending:
	  1. Ownership — the device must belong to current_user
	  2. Eligibility — device.var_maps must contain mapping.property_name
	  3. Duplicate — the device must not already be assigned to this mapping
	Invalid or ineligible device IDs are silently skipped.
	The mapping ownership check is done once before the loop.
	"""
	# Parse JSON body — bail immediately if malformed or missing
	data = request.get_json(silent=True)
	if not data:
		return jsonify({"status": "error", "message": "Invalid request"}), 400
	mapping_id = data.get("mapping_id", None)
	device_ids = data.get("device_ids", [])

	if not device_ids:
		return jsonify(
			{"status": "error", "message": "No devices provided"}), 400

	# mapping_id comes from JSON, not a URL parameter — manual UUID cast needed
	try:
		parsed_mapping_id = uuid.UUID(mapping_id)
	except (ValueError, TypeError):
		return jsonify(
			{"status": "error", "message": "Invalid mapping ID"}), 400

	with get_session() as db_session:
		# Ownership check on mapping — done once before the loop
		mapping = db_session.query(VariableMapping).filter_by(
			id=parsed_mapping_id, user_id=current_user.id).first()
		if not mapping:
			return jsonify(
				{"status": "error", "message": "Mapping not found"}), 404

		# Snapshot already-assigned IDs before the loop to avoid re-querying
		# the relationship on every iteration
		assigned_ids = {d.id for d in mapping.devices}

		for device_id_str in device_ids:
			# Parse each device UUID — skip silently if malformed
			try:
				device = db_session.query(Inventory).filter_by(
					id=uuid.UUID(device_id_str),
					user_id=current_user.id).first()
			except (ValueError, TypeError):
				continue
			# Skip if device not found or not owned by current_user
			if not device:
				continue
			# Eligibility check — device must have the mapped attribute set,
			# and the value must be truthy (empty string/list would produce
			# garbage substitution at rollout time)
			if not (device.var_maps or {}).get(mapping.property_name):
				continue
			# Skip if already assigned to avoid duplicate join table rows
			if device.id in assigned_ids:
				continue
			mapping.devices.append(device)

	audit("mapping.bulk_assign", object_type="VariableMapping",
	      object_id=parsed_mapping_id, detail={"count": len(device_ids)})
	return jsonify({"status": "success"})


# ── Property Definitions ─────────────────────────────────────────────────────

@app.route("/properties")
@login_required
def properties():
	sys_props, user_props = get_property_defs(current_user.id)
	return render_template("properties.html", sys_props=sys_props,
	                       user_props=user_props, active_section="properties")


@app.route("/properties/create", methods=["POST"])
@login_required
def properties_create():
	data = request.get_json(silent=True) or {}
	name = data.get("name", "").strip().lower().replace(" ", "_")
	label = data.get("label", "").strip()
	icon = data.get("icon", "bi-tag").strip() or "bi-tag"
	is_list = bool(data.get("is_list", False))
	if not name or not label:
		return jsonify(
			{"status": "error", "message": "Name and label are required."}), 400
	with get_session() as db_session:
		existing = db_session.query(PropertyDefinition).filter_by(
			name=name, user_id=current_user.id).first()
		if existing:
			return jsonify({"status": "error",
			                "message": "Property name already exists."}), 400
		# Also block shadowing system property names
		sys_names = {p["name"] for p in SYSTEM_PROPERTIES}
		if name in sys_names:
			return jsonify({"status": "error",
			                "message": "Cannot shadow a system property."}), 400
		prop = PropertyDefinition(name=name, label=label, icon=icon,
		                          is_list=is_list, user_id=current_user.id)
		db_session.add(prop)
		db_session.flush()
		prop_id = str(prop.id)
	audit("property.create", object_type="PropertyDefinition",
	      object_id=uuid.UUID(prop_id), object_label=name)
	return jsonify({"status": "ok", "id": prop_id, "name": name,
	                "label": label, "icon": icon, "is_list": is_list})


@app.route("/properties/quick_create", methods=["POST"])
@login_required
def properties_quick_create():
	return properties_create()


@app.route("/properties/<uuid:prop_id>/edit", methods=["POST"])
@login_required
def properties_edit(prop_id):
	data = request.get_json(silent=True) or {}
	label = data.get("label", "").strip()
	icon = data.get("icon", "bi-tag").strip() or "bi-tag"
	is_list = bool(data.get("is_list", False))
	if not label:
		return jsonify(
			{"status": "error", "message": "Label is required."}), 400
	with get_session() as db_session:
		prop = db_session.query(PropertyDefinition).filter_by(
			id=prop_id, user_id=current_user.id).first()
		if not prop:
			return jsonify({"status": "error", "message": "Not found."}), 404
		prop.label = label
		prop.icon = icon
		prop.is_list = is_list
		prop_name = prop.name
	audit("property.edit", object_type="PropertyDefinition",
	      object_id=prop_id, object_label=prop_name)
	return jsonify({"status": "ok"})


@app.route("/properties/<uuid:prop_id>/delete", methods=["POST"])
@login_required
def properties_delete(prop_id):
	with get_session() as db_session:
		prop = db_session.query(PropertyDefinition).filter_by(
			id=prop_id, user_id=current_user.id).first()
		if not prop:
			return jsonify({"status": "error", "message": "Not found."}), 404
		prop_name = prop.name
		db_session.delete(prop)
	audit("property.delete", object_type="PropertyDefinition",
	      object_id=prop_id, object_label=prop_name)
	return jsonify({"status": "ok"})


@app.route("/admin/active_job_count")
@login_required
def admin_active_job_count():
	if current_user.role != "admin":
		return jsonify({"status": "error"}), 403
	with get_session() as db_session:
		count = db_session.query(RolloutSession).count()
	return jsonify({"count": count})


@app.route("/admin/restart", methods=["POST"])
@login_required
def admin_restart():
	if current_user.role != "admin":
		return jsonify({"status": "error", "message": "Forbidden"}), 403
	audit("server.restart", object_type="Server", object_label="webapp")

	def _do_restart():
		time.sleep(1.5)
		import subprocess
		subprocess.Popen([sys.executable] + sys.argv)
		os._exit(0)

	threading.Thread(target=_do_restart, daemon=True).start()
	return jsonify({"status": "ok"})


if __name__ == "__main__":
	serve(app, host="0.0.0.0", port=8080)
