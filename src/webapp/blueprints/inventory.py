# python utilities
import os
import tempfile
import uuid

# services
# flask
from flask import Blueprint, render_template, request, current_app, redirect, \
	flash, url_for
from flask_login import current_user, login_required

# local modules
from src.db.tables import VariableMapping, Inventory, SecurityProfile, User
from src.input_parser import InputParser
from src.logging_utils import RolloutLogger
from src.validation import Validator
from src.webapp.utils import ok, err, with_form, with_json, flash_redirect

bp = Blueprint('inventory', __name__, url_prefix='/inventory')

#TODO  add uuid safety and raise on type error
##############################Routes#######################################
@bp.route("")
@login_required
def inventory():
	sys_props, user_props = current_app.web.get_property_defs(current_user.id)
	with current_app.backend.postgres.get_session() as db_session:
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


@bp.route("/create", methods=["POST"])
@login_required
@with_form("ip", "device_type")
def inventory_create(data):
	label = data.get("label", "").strip()
	ip = data.get("ip", "").strip()
	port = data.get("port", "22").strip()
	device_type = data.get("device_type", "").strip()
	sec_profile_id = data.get("sec_profile_id", "").strip()

	with current_app.backend.postgres.get_session() as db_session:
		row = Inventory(
			user_id=current_user.id,
			label=label,
			ip=ip,
			port=int(port),
			device_type=device_type,
			sec_profile_id=uuid.UUID(sec_profile_id) if sec_profile_id else None
		)
		db_session.add(row)

	current_app.web.audit("inventory.create", object_type="Inventory",
	                      object_label=label)
	flash(f"{label} added to inventory.", "success")
	return redirect(url_for("inventory.inventory"))


@bp.route("/test_connection", methods=["POST"])
@login_required
@with_json()
def inventory_test_connection(data):
	ip = str(data.get("ip", "")).strip()
	port = str(data.get("port", "")).strip()

	if not Validator.validate_ip(ip):
		return err("Invalid IP address")
	if not Validator.validate_port(port):
		return err("Port must be between 1 and 65535")

	if Validator.test_tcp_port(ip, int(port)):
		return ok(f"TCP port {port} reachable on {ip}")
	return err(f"TCP port {port} unreachable on {ip}")


@bp.route("/<uuid:device_id>/edit", methods=["POST"])
@login_required
def inventory_edit(device_id):
	def _edit(device, db_session):
		device.label = request.form.get("label", "").strip()
		device.ip = request.form.get("ip", "").strip()
		device.port = int(request.form.get("port", 22))
		device.device_type = request.form.get("device_type", "").strip()
		sec_profile_id = request.form.get("sec_profile_id", "").strip()
		device.sec_profile_id = uuid.UUID(
			sec_profile_id) if sec_profile_id else None
		sys_props, user_props = current_app.web.get_property_defs(
			current_user.id)
		all_props = {p["name"]: p for p in sys_props + user_props}
		var_maps = {}
		for inv_key, inv_val in request.form.items():
			if not inv_key.startswith("attr_"):
				continue
			prop_name = inv_key[5:]
			inv_val = inv_val.strip()
			if not inv_val:
				continue
			if all_props.get(prop_name, {}).get("is_list"):
				var_maps[prop_name] = [v.strip() for v in inv_val.split(",") if
				                       v.strip()]
			else:
				var_maps[prop_name] = inv_val
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
		current_app.web.audit("inventory.edit", object_type="Inventory",
		                      object_id=device_id, object_label=device.label)
		return flash_redirect(f"{device.label} updated.", "inventory.inventory")

	return current_app.web.act_on_db_obj(Inventory, device_id, _edit,
	                                     user_id=current_user.id,
	                                     on_missing=lambda: flash_redirect(
		                                     "Device not found.",
		                                     "inventory.inventory",
		                                     "danger"))


@bp.route("/<uuid:device_id>/delete", methods=["POST"])
@login_required
def inventory_delete(device_id):
	return current_app.web.act_on_db_obj(
		Inventory, device_id,
		current_app.web.delete_op("inventory.delete",
		                          on_success=lambda label: flash_redirect(
			                          f"{label} removed from inventory.",
			                          "inventory.inventory")),
		user_id=current_user.id,
		on_missing=lambda: flash_redirect("Device not found.",
		                                  "inventory.inventory",
		                                  "danger")
	)


@bp.route("/import_csv", methods=["POST"])
@login_required
def inventory_import_csv():
	"""
	Bulk-imports devices from an uploaded CSV file into the user's inventory.
	Saves the upload to a temp file, delegates to InputParser.csv_to_inventory,
	which validates each row and TCP-checks each device before writing.
	NOTE: TCP checks are sequential — large CSVs will block the web process.
		  Phase 3.6 per-device concurrency will address this.
	Per-device errors are returned as a list from csv_to_inventory
	and flashed to the user.
	"""
	csv_file = request.files.get("csv_file")
	if not csv_file or not csv_file.filename:
		flash("No file selected.", "danger")
		return redirect(url_for("inventory.inventory"))

	label = request.form.get("label", "").strip() or None

	# Save upload to a temp file — csv_to_inventory takes a path, not a file object
	with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
		tmp_path = tmp.name
		csv_file.save(tmp_path)

	try:
		logger = RolloutLogger(webapp=False, verbose=False,
		                       prefix="csv_import",
		                       job_id=str(uuid.uuid4())[:8])
		validator = Validator(logger)
		parser = InputParser(validator, logger)

		with current_app.backend.postgres.get_session() as db_session:
			devices, errors = parser.csv_to_inventory(
				tmp_path, current_user.id, db_session, label=label)

		if errors:
			for msg in errors:
				flash(msg, "danger")
		if devices:
			current_app.web.audit("inventory.import_csv",
			                      detail={"count": len(devices)})
			flash(
				f"{len(devices)} device{'s' if len(devices) != 1 else ''}"
				f" imported successfully.",
				"success")
		elif not errors:
			flash("No valid devices found in CSV.", "warning")

	finally:
		os.unlink(tmp_path)

	return redirect(url_for("inventory.inventory"))


@bp.route("/bulk_assign", methods=["POST"])
@login_required
def inventory_bulk_assign():
	logger = RolloutLogger(webapp=False, verbose=False,
	                       prefix="bulk_sec_assign",
	                       job_id=str(uuid.uuid4())[:8])
	data = request.get_json(silent=True)
	if not data:
		logger.notify("Bulk assign failed: invalid ldap_request", "red",
		              important=True)
		return err("Invalid ldap_request")

	profile_id = data.get("profile_id")
	device_ids = data.get("device_ids", [])

	if not device_ids:
		logger.notify("Bulk assign failed: no devices provided", "red",
		              important=True)
		return err("No devices provided")

	parsed_profile_id = uuid.UUID(profile_id) if profile_id else None
	logger.notify(
		f"Bulk security assign started: {len(device_ids)} devices → profile {profile_id or 'unassign'}",
		important=True)

	with current_app.backend.postgres.get_session() as db_session:
		if parsed_profile_id:
			profile = db_session.query(SecurityProfile).filter_by(
				id=parsed_profile_id, user_id=current_user.id).first()
			if not profile:
				logger.notify("Bulk assign failed: profile not found", "red",
				              important=True)
				return err("Profile not found", 404)

		assigned, skipped = 0, 0
		for device_id_str in device_ids:
			device = db_session.query(Inventory).filter_by(
				id=uuid.UUID(device_id_str), user_id=current_user.id).first()
			if device:
				device.sec_profile_id = parsed_profile_id
				logger.notify(f"{device.label} ({device.ip}): assigned",
				              "green")
				assigned += 1
			else:
				logger.notify(f"Device {device_id_str}: not found", "red")
				skipped += 1

	logger.notify(
		f"Bulk security assign complete: {assigned} assigned, {skipped} skipped",
		"green" if not skipped else "yellow", important=True)
	current_app.web.audit("inventory.bulk_assign", detail={
		"count": len(device_ids),
		"profile_id": str(profile_id) if profile_id else None})
	return ok()
