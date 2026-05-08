#python utilities
import uuid

#servics
#flask
from flask import (Blueprint, current_app, render_template, request, flash,
                   redirect, url_for)
from flask_login import current_user, login_required
#netmiko
from netmiko import ConnectHandler, NetmikoTimeoutException, \
	NetmikoAuthenticationException

#local modules
from src.core import Device
from src.db.tables import SecurityProfile, User, Inventory
from src.encryption import encrypt, decrypt
from src.validation import Validator
from src.webapp.utils import ok, err, with_json, with_form, flash_redirect

bp = Blueprint('security', __name__, url_prefix='/security')

#######################Routes###############################
@bp.route("")
@login_required
def security():
	with current_app.backend.postgres.get_session() as db_session:
		user = db_session.get(User, current_user.id)
		profiles = user.security_profiles
		_ = [p.inventory for p in profiles]
		devices = user.inventory
		db_session.expunge_all()

	return render_template("security.html",
	                       profiles=profiles,
	                       devices=devices,
	                       active_section="security")


@bp.route("/create", methods=["POST"])
@login_required
@with_form("username", "password")
def security_create(data):
	label = data.get("label", "").strip() or None
	username = data.get("username", "").strip()
	password = data.get("password", "").strip()
	enable_secret = data.get("enable_secret", "").strip() or None

	current_app.web.build_security_profile(label, username, password,
	                                       enable_secret, current_user.id)
	flash("Security profile created.", "success")
	return redirect(url_for("security.security"))


@bp.route("/quick_create", methods=["POST"])
@login_required
@with_json()
def security_quick_create(data):
	label = str(data.get("label", "") or "").strip() or None
	username = str(data.get("username", "") or "").strip()
	password = str(data.get("password", "") or "")
	enable_secret = str(data.get("enable_secret", "") or "").strip() or None
	if not username or not password:
		return err("Username and password are required", 422)

	profile_id = current_app.web.build_security_profile(label, username,
	                                                    password,
	                                                    enable_secret,
	                                                    current_user.id)
	return ok(id=profile_id, label=label or username)


@bp.route("/<uuid:profile_id>/edit", methods=["POST"])
@login_required
def security_edit(profile_id):
	def _edit(profile, _):
		profile.label = request.form.get("label", "").strip() or None
		profile.username = request.form["username"]
		new_password = request.form.get("password", "").strip()
		if new_password:
			profile.password_secret = encrypt(new_password)
		new_secret = request.form.get("enable_secret", "").strip()
		if new_secret:
			profile.enable_secret = encrypt(new_secret)
		elif request.form.get("clear_enable_secret"):
			profile.enable_secret = None
		current_app.web.audit("security_profile.edit",
		                      object_type="SecurityProfile",
		                      object_id=profile_id)
		return flash_redirect("Security profile updated.", "security.security")

	return current_app.web.act_on_db_obj(SecurityProfile, profile_id, _edit,
	                                     user_id=current_user.id,
	                                     on_missing=lambda: redirect(
		                                     url_for("security.security")))


@bp.route("/<uuid:profile_id>/delete", methods=["POST"])
@login_required
def security_delete(profile_id):
	def _guard(p):
		if p.inventory:
			return flash_redirect(
				f"Cannot delete '{p.label or p.username}' — "
				f"{len(p.inventory)} device(s) assigned. "
				f"Delete or reassign them first.",
				"security.security", "danger")

	return current_app.web.act_on_db_obj(
		SecurityProfile, profile_id,
		current_app.web.delete_op("security_profile.delete",
		                          data_filter=_guard,
		                          label_func=lambda p: p.label or p.username,
		                          on_success=lambda _: flash_redirect(
			                          "Profile deleted.",
			                          "security.security")),
		user_id=current_user.id,
		on_missing=lambda: redirect(url_for("security.security"))
	)


@bp.route("/<uuid:profile_id>/test", methods=["POST"])
@login_required
@with_json()
def security_test(profile_id, data):
	if not data.get("device_id"):
		return err("No device selected", 404)

	try:
		device_id = uuid.UUID(data["device_id"])
	except ValueError:
		return err("Invalid ldap_request", 422)

	with current_app.backend.postgres.get_session() as db_session:
		profile = db_session.query(SecurityProfile).filter_by(
			id=profile_id, user_id=current_user.id).first()
		device = db_session.query(Inventory).filter_by(
			id=device_id, user_id=current_user.id).first()
		if not profile or not device:
			return err("Profile or device not found", 404)
		if not Validator.test_tcp_port(device.ip, device.port):
			return err(f"TCP port {device.port} unreachable on {device.ip}",
			           503)
		db_session.expunge_all()

	device_obj = Device(ip=device.ip,
	                    port=device.port,
	                    device_type=device.device_type,
	                    label=device.label,
	                    username=profile.username,
	                    password=decrypt(profile.password_secret),
	                    secret=decrypt(profile.enable_secret)
	                    if profile.enable_secret else ""
	                    )
	try:
		conn = ConnectHandler(**device_obj.netmiko_connector())
		conn.disconnect()
		return ok(f"Connected successfully to {device.ip}")
	except NetmikoAuthenticationException:
		return err("Authentication failed — check username and password", 401)
	except NetmikoTimeoutException:
		return err(f"Connection timed out on {device.ip}:{device.port}", 504)
	except Exception as e:
		return err(str(e), 500)
