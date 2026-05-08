# python utilities
import uuid

# services
# flask
from flask import Blueprint, render_template, request, current_app, redirect, \
	flash, url_for, Response
from flask_login import current_user, login_required
# sqlalchemy
from sqlalchemy.exc import IntegrityError
from werkzeug import Response

# local modules
from src.db.tables import VariableMapping, Inventory, User
from src.logging_utils import RolloutLogger
from src.validation import Validator
from src.webapp.utils import ok, err, with_form, with_json, flash_redirect

bp = Blueprint('mappings', __name__, url_prefix='/mappings')


#######################Route helpers###########################################
def validate_mapping_fields(index: int, property_name: str, inner_token: str) \
		-> Response | None:
	status, msg = Validator.validate_var_map_inner_token(inner_token)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings.mappings"))

	status, msg = Validator.validate_var_map_property_name(property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings.mappings"))

	status, msg = Validator.validate_var_index(index, property_name)
	if not status:
		flash(msg, "danger")
		return redirect(url_for("mappings.mappings"))
	return None


def parse_mapping_input(data) -> Response | dict[str, str | int]:
	label = data.get("label", "").strip() or None
	inner_token = data["token_inner"].strip().upper()
	property_name = data["property_name"]
	index = int(data.get("index", "").strip()) if data.get(
		"index", "").strip() else None

	if invalid := validate_mapping_fields(index, property_name, inner_token):
		return invalid

	return {"label": label, "property_name": property_name, "index": index,
	        "token": f"$${inner_token}$$"}


##############################Routes#######################################
@bp.route("")
@login_required
def mappings():
	with current_app.backend.postgres.get_session() as db_session:
		user = db_session.get(User, current_user.id)
		var_binds = user.variable_mappings
		_ = [m.devices for m in var_binds]
		devices = user.inventory
		db_session.expunge_all()

	sys_props, user_props = current_app.web.get_property_defs(current_user.id)
	return render_template("variable_mappings.html", mappings=var_binds,
	                       devices=devices, sys_props=sys_props,
	                       user_props=user_props, active_section="mappings")


@bp.route("/create", methods=["POST"])
@login_required
@with_form("token_inner", "property_name")
def mappings_create(data):
	parsed_data = parse_mapping_input(data)
	if isinstance(parsed_data, Response):
		return parsed_data

	token = parsed_data["token"]
	row = VariableMapping(
		label=parsed_data["label"],
		token=token,
		index=parsed_data["index"],
		property_name=parsed_data["property_name"],
		user_id=current_user.id
	)

	try:
		with current_app.backend.postgres.get_session() as db_session:
			db_session.add(row)
		current_app.web.audit("mapping.create", object_type="VariableMapping",
		                      object_label=token)
		flash("Mapping created.", "success")
	except IntegrityError:
		current_app.web.audit("mapping.create", success=False,
		                      detail={"reason": "duplicate_token",
		                              "token": token})
		flash("A mapping with that token already exists.", "danger")

	return redirect(url_for("mappings.mappings"))


@bp.route("/quick_create", methods=["POST"])
@login_required
@with_json()
def mappings_quick_create(data):
	inner_token = str(data.get("token_inner", "") or "").strip().upper()
	property_name = str(data.get("property_name", "") or "").strip()
	index_raw = data.get("index")
	index = int(index_raw) if index_raw is not None else None

	status, msg = Validator.validate_var_map_inner_token(inner_token)
	if not status:
		return err(msg)
	status, msg = Validator.validate_var_map_property_name(property_name)
	if not status:
		return err(msg)
	status, msg = Validator.validate_var_index(index, property_name)
	if not status:
		return err(msg)

	token = f"$${inner_token}$$"
	row = VariableMapping(token=token, index=index, property_name=property_name,
	                      user_id=current_user.id)
	try:
		with current_app.backend.postgres.get_session() as db_session:
			db_session.add(row)
			db_session.flush()
			mapping_id = str(row.id)
	except IntegrityError:
		current_app.web.audit("mapping.create", success=False,
		                      detail={"reason": "duplicate_token",
		                              "token": token})
		return err(f"Token {token} already exists")
	current_app.web.audit("mapping.create", object_type="VariableMapping",
	                      object_label=token)
	return ok(id=mapping_id, token=token, property_name=property_name,
	          index=index)


@bp.route("/<uuid:mapping_id>/edit", methods=["POST"])
@login_required
@with_form("token_inner", "property_name")
def mappings_edit(mapping_id, data):
	parsed_data = parse_mapping_input(data)
	if isinstance(parsed_data, Response):
		return parsed_data
	token = parsed_data["token"]

	try:
		with current_app.backend.postgres.get_session() as db_session:
			mapping = db_session.query(VariableMapping).filter_by(
				id=mapping_id, user_id=current_user.id
			).first()

			if not mapping:
				flash("Mapping not found.", "danger")
				return redirect(url_for("mappings.mappings"))

			mapping.label = parsed_data["label"]
			mapping.token = token
			mapping.property_name = parsed_data["property_name"]
			mapping.index = parsed_data["index"]

		current_app.web.audit("mapping.edit", object_type="VariableMapping",
		                      object_id=mapping_id, object_label=token)
		flash("Mapping updated.", "success")
	except IntegrityError:
		current_app.web.audit("mapping.edit", success=False,
		                      detail={"reason": "duplicate_token",
		                              "token": token})
		flash("A mapping with that token already exists.", "danger")

	return redirect(url_for("mappings.mappings"))


@bp.route("/<uuid:mapping_id>/delete", methods=["POST"])
@login_required
def mappings_delete(mapping_id):
	return current_app.web.act_on_db_obj(
		VariableMapping, mapping_id,
		current_app.web.delete_op("mapping.delete",
		                          label_func=lambda m: m.token,
		                          on_success=lambda _: flash_redirect(
			                          "Mapping deleted.",
			                          "mappings.mappings")),
		user_id=current_user.id,
		on_missing=lambda: redirect(url_for("mappings.mappings"))
	)


@bp.route("/bulk_assign", methods=["POST"])
@login_required
def mappings_bulk_assign():
	"""
	Assigns a list of inventory devices to a variable mapping via the
	many-to-many join table.
	Accepts a JSON body with mapping_id and
	device_ids.
	For each device, three checks are enforced before appending:
	  1. Ownership — the device must belong to current_user
	  2. Eligibility — device.var_maps must contain mapping.property_name
	  3. Duplicate — the device must not already be assigned to this mapping
	Invalid or ineligible device IDs are silently skipped.
	The mapping ownership check is done once before the loop.
	"""
	logger = RolloutLogger(webapp=False, verbose=False,
	                       prefix="bulk_var_assign",
	                       job_id=str(uuid.uuid4())[:8])

	# Parse JSON body — bail immediately if malformed or missing
	data = request.get_json(silent=True)
	if not data:
		logger.notify("Bulk mapping assign failed: invalid ldap_request", "red",
		              important=True)
		return err("Invalid ldap_request")
	mapping_id = data.get("mapping_id", None)
	device_ids = data.get("device_ids", [])

	if not device_ids:
		logger.notify("Bulk mapping assign failed: no devices provided", "red",
		              important=True)
		return err("No devices provided")

	# mapping_id comes from JSON, not a URL parameter — manual UUID cast needed
	try:
		parsed_mapping_id = uuid.UUID(mapping_id)
	except (ValueError, TypeError):
		logger.notify("Bulk mapping assign failed: invalid mapping ID", "red",
		              important=True)
		return err("Invalid mapping ID")

	with current_app.backend.postgres.get_session() as db_session:
		# Ownership check on mapping — done once before the loop
		mapping = db_session.query(VariableMapping).filter_by(
			id=parsed_mapping_id, user_id=current_user.id).first()
		if not mapping:
			logger.notify("Bulk mapping assign failed: mapping not found",
			              "red", important=True)
			return err("Mapping not found", 404)

		logger.notify(
			f"Bulk mapping assign started: {len(device_ids)} devices → mapping {mapping.token}",
			important=True)

		# Snapshot already-assigned IDs before the loop to avoid re-querying
		# the relationship on every iteration
		assigned_ids = {d.id for d in mapping.devices}

		assigned, skipped = 0, 0
		for device_id_str in device_ids:
			# Parse each device UUID — skip silently if malformed
			try:
				device = db_session.query(Inventory).filter_by(
					id=uuid.UUID(device_id_str),
					user_id=current_user.id).first()
			except (ValueError, TypeError):
				skipped += 1
				continue
			# Skip if device not found or not owned by current_user
			if not device:
				logger.notify(f"Device {device_id_str}: not found", "red")
				skipped += 1
				continue
			# Eligibility check — device must have the mapped attribute set,
			# and the value must be truthy (empty string/list would produce
			# garbage substitution at rollout time)
			if not (device.var_maps or {}).get(mapping.property_name):
				logger.notify(
					f"{device.label} ({device.ip}): ineligible — missing attribute '{mapping.property_name}'",
					"yellow")
				skipped += 1
				continue
			# Skip if already assigned to avoid duplicate join table rows
			if device.id in assigned_ids:
				logger.notify(f"{device.label} ({device.ip}): already assigned",
				              "yellow")
				skipped += 1
				continue
			mapping.devices.append(device)
			logger.notify(f"{device.label} ({device.ip}): assigned", "green")
			assigned += 1

	logger.notify(
		f"Bulk mapping assign complete: {assigned} assigned, {skipped} skipped",
		"green" if not skipped else "yellow", important=True)
	current_app.web.audit("mapping.bulk_assign", object_type="VariableMapping",
	                      object_id=parsed_mapping_id,
	                      detail={"count": len(device_ids)})
	return ok()
