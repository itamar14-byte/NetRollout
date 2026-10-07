"""Inventory: the devices a user rolls out to (their own, and the global ones
admins publish) - add, edit, delete, a CSV import, security profiles and
variable mappings assigned in bulk, and reachability from this server."""
import os
import tempfile
import uuid
from typing import Any

from flask import Blueprint, render_template, request, redirect, flash, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy.orm import Session

from src.db.tables import VariableMapping, Inventory, SecurityProfile
from src.inventory import (can_edit_device, import_csv, partition_devices, query_visible_devices,
                           same_endpoint_devices, same_endpoint_warning, visible_devices_clause)
from src.rollout import inputs as validation
from src.rollout.engine import endpoint, mapping_resolvable
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger
from src.webapp.flask_app import current_app
from src.webapp.utils import ok, err, with_form, with_json, flash_redirect, signed_in_user

bp = Blueprint('inventory', __name__, url_prefix='/inventory')


##############################Route Helpers################################
def parse_mapping_ids(raw_ids: list[str]) -> list[uuid.UUID]:
	""":raises ValueError: a malformed id"""
	return [uuid.UUID(mid) for mid in raw_ids]


def set_user_mappings(device: Inventory, user_id: uuid.UUID,
                      mapping_ids: list[uuid.UUID], db_session: Session) -> list[str]:
	"""The device's bindings to the user's mappings become `mapping_ids`. The
	join table is shared across users: only this user's bindings are
	replaced, others' on a global device stay. A mapping the device can't
	resolve (attribute missing, list index out of range) isn't bound.

	:returns: the tokens not bound, for the caller to tell the user"""
	selected = db_session.query(VariableMapping).filter(
		VariableMapping.id.in_(mapping_ids),
		VariableMapping.user_id == user_id
	).all() if mapping_ids else []
	eligible = [m for m in selected if mapping_resolvable(
		device.var_maps, m.property_name, m.index)]
	device.var_mappings = [m for m in device.var_mappings
	                       if m.user_id != user_id] + eligible
	return sorted(m.token for m in selected if m not in eligible)


def flash_skipped_mappings(skipped: list[str]) -> None:
	if skipped:
		flash(f"Not bound — the device has no value for their attribute: "
		      f"{', '.join(skipped)}", "warning")


def profile_allowed(profile_id: uuid.UUID | None, db_session: Session,
                    current_profile_id: uuid.UUID | None = None) -> bool:
	"""A device may only carry a profile the current user owns — otherwise a
	user could attach another user's (e.g. an admin's global) credentials to
	a device they control. Keeping the device's profile unchanged is always
	allowed (an admin saving another admin's global device); None (no
	profile) too."""
	if profile_id is None or profile_id == current_profile_id:
		return True
	return db_session.query(SecurityProfile).filter_by(
		id=profile_id, user_id=current_user.id).first() is not None


def device_problem(ip: str, port: str, device_type: str) -> str | None:
	"""The server's check of a device's fields (the page's can be bypassed).

	:returns: what's wrong, in words; None when they're valid"""
	if not validation.validate_ip(ip):
		return f"Not a valid IP address: {ip}."
	if not validation.validate_port(port):
		return "The port is a number from 1 to 65535."
	if not validation.validate_platform(device_type):
		return f"Unsupported device type: {device_type}."
	return None


def device_not_found() -> ResponseReturnValue:
	return flash_redirect("Device not found.", "inventory.inventory", "danger")


##############################Routes#######################################
@bp.route("")
@login_required
def inventory() -> str:
	"""The inventory page: the user's devices and the global ones, with
	their profiles, mappings and properties."""
	sys_props, user_props = current_app.web.get_property_defs(current_user.id)
	with current_app.backend.postgres.get_session() as db_session:
		devices = query_visible_devices(db_session, current_user.id)
		user = signed_in_user(db_session)
		profiles = user.security_profiles
		var_mappings = user.variable_mappings
		db_session.expunge_all()
	global_devices, my_devices = partition_devices(devices)
	return render_template("inventory.html",
	                       global_devices=global_devices,
	                       my_devices=my_devices,
	                       is_admin=current_user.role == "admin",
	                       profiles=profiles,
	                       mappings=var_mappings,
	                       sys_props=sys_props,
	                       user_props=user_props,
	                       active_section="inventory")


@bp.route("/create", methods=["POST"])
@login_required
@with_form("ip", "device_type")
def inventory_create(data: Any) -> ResponseReturnValue:
	"""Add a device from the page's form. Only an admin may make it global,
	and a global one needs a security profile; an ip:port in use already is
	allowed, with a warning."""
	ip = data.get("ip", "").strip()
	port = data.get("port", "22").strip()
	device_type = data.get("device_type", "").strip()
	if problem := device_problem(ip, port, device_type):
		return flash_redirect(problem, "inventory.inventory", "danger")
	ip = validation.normalize_ip(ip)
	label = data.get("label", "").strip() or ip
	sec_profile_id = data.get("sec_profile_id", "").strip()
	try:
		parsed_sec_id = uuid.UUID(sec_profile_id) if sec_profile_id else None
	except ValueError:
		return err("Invalid security profile ID", 422)
	# Only admins may publish a device globally; the field is ignored otherwise
	is_global = current_user.role == "admin" and data.get("is_global") == "on"
	if is_global and not parsed_sec_id:
		return flash_redirect("A global device needs a security profile — "
		                      "users can't assign their own to it.",
		                      "inventory.inventory", "danger")

	with current_app.backend.postgres.get_session() as db_session:
		if not profile_allowed(parsed_sec_id, db_session):
			return flash_redirect("Security profile not found.",
			                      "inventory.inventory", "danger")
		duplicate = same_endpoint_warning(
			same_endpoint_devices(db_session, current_user.id, ip, port),
			ip, port)
		row = Inventory(
			user_id=current_user.id,
			label=label,
			ip=ip,
			port=int(port),
			device_type=device_type,
			sec_profile_id=parsed_sec_id,
			is_global=is_global
		)
		db_session.add(row)

	current_app.web.audit("inventory.create", object_type="Inventory",
	                      object_label=label,
	                      detail={"is_global": is_global})
	flash(f"{label} added to inventory.", "success")
	if duplicate:
		flash(duplicate, "warning")
	return redirect(url_for("inventory.inventory"))


@bp.route("/test_connection", methods=["POST"])
@login_required
@with_json()
def inventory_test_connection(data: dict[str, Any]) -> ResponseReturnValue:
	"""Is the TCP port open from this server? JSON {ip, port}."""
	ip = str(data.get("ip", "")).strip()
	port = str(data.get("port", "")).strip()

	if not validation.validate_ip(ip):
		return err("Invalid IP address")
	if not validation.validate_port(port):
		return err("Port must be between 1 and 65535")

	if validation.tcp_reachable(ip, int(port)):
		return ok(f"TCP port {port} reachable on {ip}")
	return err(f"TCP port {port} unreachable on {ip}")


MAX_REACHABILITY_BATCH = 500


@bp.route("/reachability", methods=["POST"])
@login_required
@with_json()
def inventory_reachability(data: dict[str, Any]) -> ResponseReturnValue:
	"""Reachability of visible devices from this server (cached briefly;
	refresh=true re-probes). {"statuses": {device_id: {reachable,
	checked_at}}}"""
	raw_ids = data.get("device_ids") or []
	if not isinstance(raw_ids, list) or len(raw_ids) > MAX_REACHABILITY_BATCH:
		return err("Invalid request", 422)
	try:
		ids = [uuid.UUID(str(i)) for i in raw_ids]
	except ValueError:
		return err("Invalid device ID", 422)
	with current_app.backend.postgres.get_session() as db_session:
		rows = db_session.query(Inventory.id, Inventory.ip, Inventory.port) \
			.filter(Inventory.id.in_(ids),
			        visible_devices_clause(current_user.id)).all()
	results = current_app.web.reachability.check(
		[(r.ip, r.port) for r in rows], refresh=bool(data.get("refresh")))
	return ok(statuses={str(r.id): results[(r.ip, int(r.port))] for r in rows})


@bp.route("/<uuid:device_id>/edit", methods=["POST"])
@login_required
def inventory_edit(device_id: uuid.UUID) -> ResponseReturnValue:
	"""Save a device from the page's form: its fields, profile, attributes
	and the user's mapping bindings - by its owner, or an admin for a
	global one. Making a global device local drops other users' bindings."""
	def _edit(device: Inventory, db_session: Session) -> ResponseReturnValue:
		# Validate everything before mutating — get_session commits on a
		# normal return, so an early error must not leave a half-applied edit.
		ip = request.form.get("ip", "").strip()
		port = request.form.get("port", "22").strip()
		device_type = request.form.get("device_type", "").strip()
		if problem := device_problem(ip, port, device_type):
			return flash_redirect(problem, "inventory.inventory", "danger")
		ip = validation.normalize_ip(ip)
		sec_profile_id = request.form.get("sec_profile_id", "").strip()
		try:
			parsed_sec_id = uuid.UUID(sec_profile_id) if sec_profile_id else None
			mapping_ids = parse_mapping_ids(request.form.getlist("mapping_ids"))
		except ValueError:
			return err("Invalid security profile or mapping ID", 422)
		if not profile_allowed(parsed_sec_id, db_session,
		                       device.sec_profile_id):
			return flash_redirect("Security profile not found.",
			                      "inventory.inventory", "danger")
		was_global = device.is_global
		# Only admins may change global status; the field is ignored otherwise
		is_global = (request.form.get("is_global") == "on"
		             if current_user.role == "admin" else was_global)
		if is_global and not parsed_sec_id:
			return flash_redirect("A global device needs a security profile — "
			                      "users can't assign their own to it.",
			                      "inventory.inventory", "danger")

		old_endpoint = (device.ip, device.port)
		device.label = request.form.get("label", "").strip() or ip
		device.ip = ip
		device.port = int(port)
		# Warn only when the endpoint changed — not on every save
		duplicate = None
		if (device.ip, device.port) != old_endpoint:
			duplicate = same_endpoint_warning(same_endpoint_devices(
				db_session, current_user.id, device.ip, device.port,
				exclude_id=device.id), device.ip, device.port)
		device.device_type = device_type
		device.sec_profile_id = parsed_sec_id
		device.is_global = is_global
		sys_props, user_props = current_app.web.get_property_defs(
			current_user.id)
		all_props = {p["name"]: p for p in sys_props + user_props}
		var_maps: dict[str, str | list[str]] = {}
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
		flash_skipped_mappings(
			set_user_mappings(device, current_user.id, mapping_ids, db_session))
		if was_global and not is_global:
			# Other users can no longer see this device — drop their bindings
			# rather than leave invisible orphans in the join table.
			device.var_mappings = [m for m in device.var_mappings
			                       if m.user_id == device.user_id]
		current_app.web.audit("inventory.edit", object_type="Inventory",
		                      object_id=device_id, object_label=device.label,
		                      detail={"is_global": is_global})
		if is_global != was_global:
			current_app.web.audit(
				"inventory.globalize" if is_global else "inventory.localize",
				object_type="Inventory", object_id=device_id,
				object_label=device.label)
		flash(f"{device.label} updated.", "success")
		if duplicate:
			flash(duplicate, "warning")
		return redirect(url_for("inventory.inventory"))

	return current_app.web.act_on_db_obj(
		Inventory, device_id, _edit,
		can_access=lambda d: can_edit_device(d, current_user),
		on_missing=device_not_found)


@bp.route("/<uuid:device_id>/mappings", methods=["POST"])
@login_required
def inventory_mappings(device_id: uuid.UUID) -> ResponseReturnValue:
	"""Bind / unbind the user's own mappings on any device they see - the
	only way to on a global device they can't edit."""
	try:
		mapping_ids = parse_mapping_ids(request.form.getlist("mapping_ids"))
	except ValueError:
		return flash_redirect("Invalid mapping ID.", "inventory.inventory",
		                      "danger")

	def _set_mappings(device: Inventory, db_session: Session) -> ResponseReturnValue:
		flash_skipped_mappings(
			set_user_mappings(device, current_user.id, mapping_ids, db_session))
		current_app.web.audit("inventory.mappings", object_type="Inventory",
		                      object_id=device_id, object_label=device.label,
		                      detail={"mapping_count": len(mapping_ids)})
		return flash_redirect(f"Mappings updated for {device.label}.",
		                      "inventory.inventory")

	return current_app.web.act_on_db_obj(
		Inventory, device_id, _set_mappings,
		can_access=lambda d: d.user_id == current_user.id or d.is_global,
		on_missing=device_not_found)


@bp.route("/<uuid:device_id>/delete", methods=["POST"])
@login_required
def inventory_delete(device_id: uuid.UUID) -> ResponseReturnValue:
	"""Delete a device - its owner, or an admin for a global one."""
	return current_app.web.act_on_db_obj(
		Inventory, device_id,
		current_app.web.delete_op("inventory.delete",
		                          on_success=lambda label: flash_redirect(
			                          f"{label} removed from inventory.",
			                          "inventory.inventory")),
		can_access=lambda d: can_edit_device(d, current_user),
		on_missing=device_not_found
	)


@bp.route("/import_csv", methods=["POST"])
@login_required
def inventory_import_csv() -> ResponseReturnValue:
	"""
	Bulk-imports devices from an uploaded CSV file into the user's inventory.
	Saves the upload to a temp file and delegates to
	InputParser.csv_to_inventory: attribute columns become variable
	attributes, credential columns become security profiles (when the
	checkbox is on), other columns are reported. No reachability check —
	Inventory shows it live. Row errors and notices are flashed.
	"""
	csv_file = request.files.get("csv_file")
	if not csv_file or not csv_file.filename:
		flash("No file selected.", "danger")
		return redirect(url_for("inventory.inventory"))

	label = request.form.get("label", "").strip() or None
	create_profiles = request.form.get("create_profiles") == "on"
	sys_props, user_props = current_app.web.get_property_defs(current_user.id)

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
			# endpoints already in use, read before the import adds rows
			in_use = {(d.ip, d.port) for d in db_session.query(
				Inventory.ip, Inventory.port).filter(
				visible_devices_clause(current_user.id))}
			report = import_csv(
				parser, tmp_path, current_user.id, db_session, label=label,
				properties=sys_props + user_props,
				create_profiles=create_profiles)

		devices = report.devices
		# same ip:port as an existing visible device, or twice in the file:
		# allowed (NAT, VRFs, labs), but say so
		seen: set[tuple[str, int]] = set()
		shared: list[str] = []
		for d in devices:
			target = (d.ip, d.port)
			if target in in_use or target in seen:
				shared.append(endpoint(d.ip, d.port))
			seen.add(target)
		if shared:
			unique = list(dict.fromkeys(shared))
			report.notices.append(("warning",
				f"{len(shared)} imported device{'s' if len(shared) != 1 else ''} "
				f"share an ip:port with another device: "
				f"{', '.join(unique[:5])}{', …' if len(unique) > 5 else ''}. "
				f"That's fine for NAT, VRFs or port-forwarded labs, but they "
				f"can't be in the same rollout."))
		for msg in report.errors:
			flash(msg, "danger")
		# same audit action as a manually created profile, marked by source
		for profile_id, profile_label in report.created_profiles:
			current_app.web.audit("security_profile.create",
			                      object_type="SecurityProfile",
			                      object_id=profile_id,
			                      object_label=profile_label,
			                      detail={"source": "csv_import"})
		if devices:
			current_app.web.audit("inventory.import_csv", detail={
				"count": len(devices),
				"profiles_created": len(report.created_profiles)})
			flash(
				f"{len(devices)} device{'s' if len(devices) != 1 else ''}"
				f" imported successfully.",
				"success")
		elif not report.errors:
			flash("No valid devices found in CSV.", "warning")
		for category, msg in report.notices:
			flash(msg, category)

	finally:
		os.unlink(tmp_path)

	return redirect(url_for("inventory.inventory"))


@bp.route("/bulk_assign", methods=["POST"])
@login_required
def inventory_bulk_assign() -> ResponseReturnValue:
	"""Assign one of the user's security profiles to their devices, or none
	(a global device keeps one): JSON {profile_id, device_ids}. Bad or
	foreign ids are skipped, each one logged."""
	logger = RolloutLogger(webapp=False, verbose=False,
	                       prefix="bulk_sec_assign",
	                       job_id=str(uuid.uuid4())[:8])
	data = request.get_json(silent=True)
	if not data:
		logger.notify("Bulk assign failed: invalid request", "red",
		              important=True)
		return err("Invalid request")

	profile_id = data.get("profile_id")
	device_ids = data.get("device_ids", [])

	if not device_ids:
		logger.notify("Bulk assign failed: no devices provided", "red",
		              important=True)
		return err("No devices provided")

	try:
		parsed_profile_id = uuid.UUID(profile_id) if profile_id else None
	except ValueError:
		return err("Invalid profile ID", 422)
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
			try:
				parsed_device_id = uuid.UUID(device_id_str)
			except (ValueError, TypeError):
				skipped += 1
				continue
			device = db_session.query(Inventory).filter_by(
				id=parsed_device_id, user_id=current_user.id).first()
			if device and device.is_global and not parsed_profile_id:
				# same rule as create/edit: a global device must keep a
				# profile — other users can't give it one
				logger.notify(f"{device.label} ({device.ip}): global device "
				              f"must keep a profile", "yellow")
				skipped += 1
			elif device:
				device.sec_profile_id = parsed_profile_id
				logger.notify(f"{device.label} ({device.ip}): "
				              f"{'assigned' if parsed_profile_id else 'unassigned'}",
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
