"""Variable mappings: a $$TOKEN$$ in the commands bound to a device
property (or one item of a list property), resolved per device at rollout -
create, edit, delete, and assign devices to one."""
import uuid
from typing import Any

from flask import Blueprint, render_template, request, redirect, flash, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from src.accounts.users import signed_in_user
from src.db.tables import VariableMapping, Inventory, PropertyDefinition
from src.inventory import visible_devices_clause, query_visible_devices, partition_devices
from src.rollout import inputs as validation
from src.rollout.engine import mapping_resolvable
from src.rollout.log import RolloutLogger
from src.webapp.app import current_app
from src.webapp.http import ok, err, with_form, with_json, flash_redirect, SYSTEM_PROPERTIES


bp = Blueprint('mappings', __name__, url_prefix='/mappings')


#######################Route helpers###########################################
def property_rules() -> tuple[set[str], set[str]]:
	"""(allowed names, list names), from the same definitions the pages show:
	the system's and the user's own properties."""
	sys_props, user_props = current_app.web.get_property_defs(current_user.id)
	props = sys_props + user_props
	return ({p["name"] for p in props},
	        {p["name"] for p in props if p["is_list"]})


def validate_mapping_fields(index: int | None, property_name: str,
                            inner_token: str) -> ResponseReturnValue | None:
	"""Check a mapping from the page's form: the token, the property, the
	index (only a list property takes one).

	:returns: None when valid; else the way back to the page, the reason
	 flashed"""
	allowed, list_props = property_rules()
	for valid, why in (validation.validate_var_map_inner_token(inner_token),
	                   validation.validate_var_map_property_name(property_name, allowed),
	                   validation.validate_var_index(index, property_name, list_props)):
		if not valid:
			flash(why or "Invalid mapping.", "danger")
			return redirect(url_for("mappings.mappings"))
	return None


def parse_index(raw: object) -> int | None:
	"""A mapping's index as typed (form text, or a JSON number).

	:returns: the index; None when blank (the whole value)
	:raises ValueError: it isn't a whole number - the message is for people"""
	text = "" if raw is None else str(raw).strip()
	if not text:
		return None
	try:
		return int(text)
	except ValueError:
		raise ValueError("The index is a number.") from None


def parse_mapping_input(data: Any) -> ResponseReturnValue | dict[str, Any]:
	"""The page's mapping form, checked.

	:returns: {label, property_name, index, token ($$...$$)}; or, invalid,
	 the way back to the page"""
	label = data.get("label", "").strip() or None
	inner_token = data["token_inner"].strip().upper()
	property_name = data["property_name"]
	try:
		index = parse_index(data.get("index"))
	except ValueError as e:
		flash(str(e), "danger")
		return redirect(url_for("mappings.mappings"))

	if invalid := validate_mapping_fields(index, property_name, inner_token):
		return invalid

	return {"label": label, "property_name": property_name, "index": index,
	        "token": f"$${inner_token}$$"}


##############################Routes#######################################
@bp.route("")
@login_required
def mappings() -> str:
	"""The mappings page: the user's mappings with their devices, and the
	devices they may assign."""
	with current_app.backend.postgres.get_session() as db_session:
		user = signed_in_user(db_session)
		var_binds = user.variable_mappings
		_ = [m.devices for m in var_binds]
		devices = query_visible_devices(db_session, current_user.id)
		db_session.expunge_all()
	global_devices, my_devices = partition_devices(devices)

	sys_props, user_props = current_app.web.get_property_defs(current_user.id)
	return render_template("variable_mappings.html", mappings=var_binds,
	                       global_devices=global_devices,
	                       my_devices=my_devices, sys_props=sys_props,
	                       user_props=user_props, active_section="mappings")


@bp.route("/create", methods=["POST"])
@login_required
@with_form("token_inner", "property_name")
def mappings_create(data: Any) -> ResponseReturnValue:
	"""A new mapping from the page's form (a token already used is refused)."""
	parsed_data = parse_mapping_input(data)
	if not isinstance(parsed_data, dict):
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
def mappings_quick_create(data: dict[str, Any]) -> ResponseReturnValue:
	"""A new mapping from the rollout page's modal: JSON {token_inner,
	property_name, index?}.

	:returns: {"status": "ok", id, token, property_name, index} or an error"""
	inner_token = str(data.get("token_inner", "") or "").strip().upper()
	property_name = str(data.get("property_name", "") or "").strip()
	try:
		index = parse_index(data.get("index"))
	except ValueError as e:
		return err(str(e), 422)

	allowed, list_props = property_rules()
	for valid, why in (validation.validate_var_map_inner_token(inner_token),
	                   validation.validate_var_map_property_name(property_name, allowed),
	                   validation.validate_var_index(index, property_name, list_props)):
		if not valid:
			return err(why or "Invalid mapping.")

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
def mappings_edit(mapping_id: uuid.UUID, data: Any) -> ResponseReturnValue:
	"""Change one of the user's mappings from the page's form."""
	parsed_data = parse_mapping_input(data)
	if not isinstance(parsed_data, dict):
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
def mappings_delete(mapping_id: uuid.UUID) -> ResponseReturnValue:
	"""Delete one of the user's mappings (its device bindings go with it)."""
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
def mappings_bulk_assign() -> ResponseReturnValue:
	"""
	Assigns a list of inventory devices to a variable mapping via the
	many-to-many join table.
	Accepts a JSON body with mapping_id, device_ids (to assign) and
	remove_ids (to unassign) — at least one list must be non-empty.
	Removal only drops this mapping's join rows: the mapping is the user's
	own, so other users' bindings on the same (global) device are untouched,
	and no visibility or eligibility check applies.
	For each device to assign, three checks are enforced before appending:
	  1. Visibility — the device must belong to current_user or be global
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
		logger.notify("Bulk mapping assign failed: invalid request", "red",
		              important=True)
		return err("Invalid request")
	mapping_id = data.get("mapping_id", None)
	device_ids = data.get("device_ids", [])
	remove_ids = data.get("remove_ids", [])

	if not device_ids and not remove_ids:
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

		removed = 0
		remove_set: set[uuid.UUID] = set()
		for device_id_str in remove_ids:
			try:
				remove_set.add(uuid.UUID(device_id_str))
			except (ValueError, TypeError):
				continue
		for unbound in [d for d in mapping.devices if d.id in remove_set]:
			mapping.devices.remove(unbound)
			logger.notify(f"{unbound.label} ({unbound.ip}): unassigned", "green")
			removed += 1

		assigned, skipped = 0, 0
		for device_id_str in device_ids:
			# Parse each device UUID — skip silently if malformed
			try:
				device: Inventory | None = db_session.query(Inventory).filter(
					Inventory.id == uuid.UUID(device_id_str),
					visible_devices_clause(current_user.id)).first()
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
			if not mapping_resolvable(device.var_maps, mapping.property_name,
			                          mapping.index):
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
		f"Bulk mapping assign complete: {assigned} assigned, {removed} "
		f"unassigned, {skipped} skipped",
		"green" if not skipped else "yellow", important=True)
	current_app.web.audit("mapping.bulk_assign", object_type="VariableMapping",
	                      object_id=parsed_mapping_id,
	                      detail={"count": assigned, "removed": removed})
	return ok()


# ══ Properties: /properties (what a mapping's variables are named) ══════════

properties_bp = Blueprint('properties', __name__, url_prefix='/properties')


##############################Routes#######################################
@properties_bp.route("")
@login_required
def properties() -> str:
	"""The properties page: the system properties and the user's own."""
	sys_props, user_props = current_app.web.get_property_defs(current_user.id)
	return render_template("properties.html", sys_props=sys_props,
	                       user_props=user_props, active_section="properties")


@properties_bp.route("/create", methods=["POST"])
@properties_bp.route("/quick_create", methods=["POST"])
@login_required
def properties_create() -> ResponseReturnValue:
	"""A new property of the user's: JSON {name, label, icon?, is_list?}. The
	name is normalised (lower case, _ for spaces); it may not repeat one of
	theirs or a system property's.

	:returns: {"status": "ok", id, name, label, icon, is_list} or an error"""
	data = request.get_json(silent=True) or {}
	name = data.get("name", "").strip().lower().replace(" ", "_")
	label = data.get("label", "").strip()
	icon = data.get("icon", "bi-tag").strip() or "bi-tag"
	is_list = bool(data.get("is_list", False))
	if not name or not label:
		return err("Name and label are required.")
	with current_app.backend.postgres.get_session() as db_session:
		existing = db_session.query(PropertyDefinition).filter_by(
			name=name, user_id=current_user.id).first()
		if existing:
			return err("Property name already exists.")
		# Also block shadowing system property names
		sys_names = {p["name"] for p in SYSTEM_PROPERTIES}
		if name in sys_names:
			return err("Cannot shadow a system property.")
		prop = PropertyDefinition(name=name, label=label, icon=icon,
		                          is_list=is_list, user_id=current_user.id)
		db_session.add(prop)
		db_session.flush()
		prop_id = str(prop.id)
	current_app.web.audit("property.create", object_type="PropertyDefinition",
	                      object_id=uuid.UUID(prop_id), object_label=name)
	return ok(id=prop_id, name=name, label=label, icon=icon, is_list=is_list)


@properties_bp.route("/<uuid:prop_id>/edit", methods=["POST"])
@login_required
def properties_edit(prop_id: uuid.UUID) -> ResponseReturnValue:
	"""Change a property's label, icon or list-ness (not its name): JSON."""
	data = request.get_json(silent=True) or {}
	label = data.get("label", "").strip()
	icon = data.get("icon", "bi-tag").strip() or "bi-tag"
	is_list = bool(data.get("is_list", False))
	if not label:
		return err("Label is required.")
	return current_app.web.act_on_db_obj(
		PropertyDefinition, prop_id,
		current_app.web.update_op({"label": label, "icon": icon, "is_list":
			is_list},
		                          "property.edit", label_func=lambda p: p.name),
		user_id=current_user.id
	)


@properties_bp.route("/<uuid:prop_id>/delete", methods=["POST"])
@login_required
def properties_delete(prop_id: uuid.UUID) -> ResponseReturnValue:
	"""Delete one of the user's properties."""
	return current_app.web.act_on_db_obj(
		PropertyDefinition, prop_id,
		current_app.web.delete_op("property.delete", label_func=lambda p:
		p.name),
		user_id=current_user.id
	)
