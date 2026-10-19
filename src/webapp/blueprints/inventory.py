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
from sqlalchemy.exc import SQLAlchemyError

from src.audit import AuditAction
from src.inventory import (DeviceFields, InventoryView, RuleRefused, SecurityProfiles,
                           form_values, import_csv, partition_devices, resolved_addresses)
from src.rollout import inputs
from src.rollout.address import is_ip, is_loopback, qualify, resolve
from src.rollout.engine import endpoint
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger, Tone
from src.webapp.app import current_app
from src.webapp.hooks import signed_in_user
from src.webapp.http import ok, err, with_form, with_json, flash_redirect, viewer

bp = Blueprint('inventory', __name__, url_prefix='/inventory')


def parse_mapping_ids(raw_ids: list[str]) -> list[uuid.UUID]:
	""":raises ValueError: a malformed id"""
	return [uuid.UUID(mid) for mid in raw_ids]


def flash_skipped_mappings(skipped: list[str]) -> None:
	if skipped:
		flash(f"Not bound - the device has no value for their attribute: "
		      f"{', '.join(skipped)}", "warning")


def device_problem(ip: str, port: str, device_type: str) -> str | None:
	"""The server's check of a device's fields (the page's can be bypassed).

	:returns: what's wrong, in words; None when they're valid"""
	if not inputs.validate_address(ip):
		return f"Not a valid IP address or hostname: {ip}."
	if not inputs.validate_port(port):
		return "The port is a number from 1 to 65535."
	if not inputs.validate_platform(device_type):
		return f"Unsupported device type: {device_type}."
	return None


def look_up(address: str) -> tuple[tuple[str, ...], str | None]:
	"""A hostname looked up before it's saved (a bounded wait): its addresses
	find the same device saved under another name or its IP.

	:returns: (its addresses - () for an IP or a failed lookup, a warning
	 for the page when it doesn't resolve - None otherwise)"""
	if is_ip(address):
		return (), None
	host = qualify(address, current_app.web.dns_suffix())
	found = resolve(host)
	if found is None:
		return (), (f"{host} doesn't resolve from this server right now - saved "
		            f"anyway; a rollout to it fails until it does.")
	return tuple(found), None


def device_not_found() -> ResponseReturnValue:
	return flash_redirect("Device not found.", "inventory.inventory", "danger")


@bp.route("")
@login_required
def inventory() -> str:
	"""The inventory page: the user's devices and the global ones, with
	the user's own profile on each (a device without one is marked ACTION
	NEEDED; ?needs_profile=1 shows only those), their mappings, properties
	and the attribute values the user sees."""
	with current_app.backend.postgres.get_session() as db_session:
		view = InventoryView(db_session, viewer())
		sys_props, user_props = view.property_defs()
		devices = view.visible()
		values = view.attributes(devices)
		my_profiles = view.profiles(devices)
		user = signed_in_user(db_session)
		profiles = user.security_profiles
		var_mappings = user.variable_mappings
		db_session.expunge_all()
	needs_profile = [d for d in devices if my_profiles[d.id] is None]
	only_needing = request.args.get("needs_profile") == "1"
	global_devices, my_devices = partition_devices(needs_profile if only_needing else devices)
	return render_template("inventory.html",
	                       global_devices=global_devices,
	                       my_devices=my_devices,
	                       my_profiles={str(i): p for i, p in my_profiles.items()},
	                       needs_profile_count=len(needs_profile),
	                       only_needing=only_needing,
	                       is_admin=viewer().is_admin,
	                       profiles=profiles,
	                       mappings=var_mappings,
	                       sys_props=sys_props,
	                       user_props=user_props,
	                       attributes=values,
	                       dns_suffix=current_app.web.dns_suffix(),
	                       active_section="inventory")


@bp.route("/create", methods=["POST"])
@login_required
@with_form("ip", "device_type")
def inventory_create(data: Any) -> ResponseReturnValue:
	"""Add a device from the page's form, with the creator's own profile
	linked (or none: ACTION NEEDED until they link one). Only an admin may
	make it global; an ip:port in use already is allowed, with a warning."""
	ip = data.get("ip", "").strip()
	port = data.get("port", "22").strip()
	device_type = data.get("device_type", "").strip()
	if problem := device_problem(ip, port, device_type):
		return flash_redirect(problem, "inventory.inventory", "danger")
	ip = inputs.normalize_address(ip)
	resolved, unresolved = look_up(ip)
	label = data.get("label", "").strip() or ip
	sec_profile_id = data.get("sec_profile_id", "").strip()
	try:
		parsed_sec_id = uuid.UUID(sec_profile_id) if sec_profile_id else None
	except ValueError:
		return err("Invalid security profile ID", 422)
	fields = DeviceFields(ip=ip, port=int(port), device_type=device_type, label=label,
	                      profile_id=parsed_sec_id, make_global=data.get("is_global") == "on",
	                      resolved=resolved)
	try:
		with current_app.backend.postgres.get_session() as db_session:
			saved = InventoryView(db_session, viewer()).save_device(None, fields)
	except RuleRefused as e:
		return flash_redirect(str(e), "inventory.inventory", "danger")

	current_app.web.audit(AuditAction.INVENTORY_CREATE, object_type="Inventory",
	                      object_label=label,
	                      detail={"is_global": saved.is_global})
	flash(f"{label} added to inventory.", "success")
	if unresolved:
		flash(unresolved, "warning")
	if saved.shared_endpoint:
		flash(saved.shared_endpoint, "warning")
	return redirect(url_for("inventory.inventory"))


@bp.route("/test_connection", methods=["POST"])
@login_required
@with_json()
def inventory_test_connection(data: dict[str, Any]) -> ResponseReturnValue:
	"""Is the TCP port open from this server? JSON {ip, port} - the address
	an IP or a hostname (a bare one with the DNS suffix)."""
	ip = str(data.get("ip", "")).strip()
	port = str(data.get("port", "")).strip()

	if not inputs.validate_address(ip):
		return err("Not a valid IP address or hostname")
	if not inputs.validate_port(port):
		return err("Port must be between 1 and 65535")

	address = inputs.normalize_address(ip)
	if is_loopback(address):
		# what's tested is NetRollout's own server, not the computer typing it
		return err(f"{address} is NetRollout's own server (in Docker, its container), not "
		           f"your computer or a device - use the device's name or address on the network")
	host = qualify(address, current_app.web.dns_suffix())
	if not is_ip(host) and resolve(host) is None:
		return err(f"Can't resolve {host} from this server")
	if inputs.tcp_reachable(host, int(port)):
		return ok(f"TCP port {port} reachable on {host}")
	return err(f"TCP port {port} unreachable on {host}")


MAX_REACHABILITY_BATCH = 500


@bp.route("/reachability", methods=["POST"])
@login_required
@with_json()
def inventory_reachability(data: dict[str, Any]) -> ResponseReturnValue:
	"""Reachability of visible devices from this server (cached briefly;
	refresh=true re-probes). {"statuses": {device_id: {reachable,
	checked_at, address (a hostname's)}}}. A hostname's address is kept as
	the device's stored address (remember_addresses)."""
	raw_ids = data.get("device_ids") or []
	if not isinstance(raw_ids, list) or len(raw_ids) > MAX_REACHABILITY_BATCH:
		return err("Invalid request", 422)
	try:
		ids = [uuid.UUID(str(i)) for i in raw_ids]
	except ValueError:
		return err("Invalid device ID", 422)
	with current_app.backend.postgres.get_session() as db_session:
		rows = InventoryView(db_session, viewer()).visible_ids(ids)
		db_session.expunge_all()
	results = current_app.web.reachability.check(
		[(r.ip, r.port) for r in rows], refresh=bool(data.get("refresh")))
	if addresses := resolved_addresses(results):
		try:
			with current_app.backend.postgres.get_session() as db_session:
				InventoryView(db_session, viewer()).remember_addresses(ids, addresses)
		except SQLAlchemyError as e:      # only the stored address: never the page's answer
			print(f"[NetRollout] couldn't save the devices' current addresses: {e}", flush=True)
	return ok(statuses={str(r.id): results[(r.ip, int(r.port))] for r in rows})


@bp.route("/<uuid:device_id>/edit", methods=["POST"])
@login_required
def inventory_edit(device_id: uuid.UUID) -> ResponseReturnValue:
	"""Save a device from the page's form: its fields, system attribute
	values, and the editor's own profile link, custom values and mapping
	bindings (other users' are never touched) - by its owner, or an admin
	for a global one. Making a global device local makes it the editor's
	and drops other users' links and bindings (InventoryView.save_device:
	the rules, all checked before anything changes)."""
	ip = request.form.get("ip", "").strip()
	port = request.form.get("port", "22").strip()
	device_type = request.form.get("device_type", "").strip()
	sec_profile_id = request.form.get("sec_profile_id", "").strip()
	if problem := device_problem(ip, port, device_type):
		return flash_redirect(problem, "inventory.inventory", "danger")
	ip = inputs.normalize_address(ip)
	# looked up before the database session: the wait holds no connection
	resolved, unresolved = look_up(ip)
	try:
		with current_app.backend.postgres.get_session() as db_session:
			view = InventoryView(db_session, viewer())
			device = view.get_editable(device_id)
			if device is None:
				return device_not_found()
			try:
				parsed_sec_id = uuid.UUID(sec_profile_id) if sec_profile_id else None
				mapping_ids = parse_mapping_ids(request.form.getlist("mapping_ids"))
			except ValueError:
				return err("Invalid security profile or mapping ID", 422)
			sys_props, user_props = view.property_defs()
			saved = view.save_device(
				device, DeviceFields(ip=ip, port=int(port), device_type=device_type,
				                     label=request.form.get("label", "").strip() or ip,
				                     profile_id=parsed_sec_id,
				                     make_global=request.form.get("is_global") == "on",
				                     resolved=resolved),
				values=form_values(request.form, sys_props + user_props),
				own=[p["name"] for p in user_props], mapping_ids=mapping_ids)
	except RuleRefused as e:
		return flash_redirect(str(e), "inventory.inventory", "danger")

	flash_skipped_mappings(saved.unbound)
	current_app.web.audit(AuditAction.INVENTORY_EDIT, object_type="Inventory",
	                      object_id=device_id, object_label=saved.label,
	                      detail={"is_global": saved.is_global})
	if saved.is_global != saved.was_global:
		current_app.web.audit(
			AuditAction.INVENTORY_GLOBALIZE if saved.is_global else AuditAction.INVENTORY_LOCALIZE,
			object_type="Inventory", object_id=device_id, object_label=saved.label,
			detail=saved.removed.detail() if saved.removed else None)
	flash(f"{saved.label} updated.", "success")
	if saved.shared_endpoint:
		flash(saved.shared_endpoint, "warning")
	return redirect(url_for("inventory.inventory"))


@bp.route("/<uuid:device_id>/attributes", methods=["POST"])
@login_required
def inventory_attributes(device_id: uuid.UUID) -> ResponseReturnValue:
	"""What anyone who sees a device sets on it for themselves - the page's
	form for a device they can't edit: their own security profile for it
	(sec_profile_id: one of theirs, blank for none; left alone when not
	sent), their own custom values (attr_<name> fields for their own
	properties; a blank one removes the value, any other field is ignored -
	the device itself never changes), then their mapping bindings, checked
	against those values. A device the user can't see: 404."""
	try:
		mapping_ids = parse_mapping_ids(request.form.getlist("mapping_ids"))
		raw_profile = request.form.get("sec_profile_id")
		profile_id = uuid.UUID(raw_profile.strip()) if raw_profile and raw_profile.strip() else None
	except ValueError:
		return flash_redirect("Invalid mapping or security profile ID.", "inventory.inventory",
		                      "danger")
	try:
		with current_app.backend.postgres.get_session() as db_session:
			view = InventoryView(db_session, viewer())
			device = view.get_visible(device_id)
			if device is None:
				return err("Not found", 404)
			if raw_profile is not None:
				view.link_profile(device, profile_id)
			_, user_props = view.property_defs()
			view.set_custom_values(device, form_values(request.form, user_props),
			                       [p["name"] for p in user_props])
			unbound = view.bind_mappings(device, mapping_ids)
			label = device.label
	except RuleRefused as e:
		return flash_redirect(str(e), "inventory.inventory", "danger")
	flash_skipped_mappings(unbound)
	current_app.web.audit(AuditAction.INVENTORY_ATTRIBUTES, object_type="Inventory",
	                      object_id=device_id, object_label=label,
	                      detail={"mapping_count": len(mapping_ids),
	                              **({"profile_linked": profile_id is not None}
	                                 if raw_profile is not None else {})})
	return flash_redirect(f"Your attributes and mappings saved for "
	                      f"{label}.", "inventory.inventory")


@bp.route("/<uuid:device_id>/delete", methods=["POST"])
@login_required
def inventory_delete(device_id: uuid.UUID) -> ResponseReturnValue:
	"""Delete a device - its owner, or an admin for a global one."""
	with current_app.backend.postgres.get_session() as db_session:
		device = InventoryView(db_session, viewer()).get_editable(device_id)
		if device is None:
			return device_not_found()
		label = device.label
		db_session.delete(device)
	current_app.web.audit(AuditAction.INVENTORY_DELETE, object_type="Inventory",
	                      object_id=device_id, object_label=label)
	return flash_redirect(f"{label} removed from inventory.", "inventory.inventory")


@bp.route("/import_csv", methods=["POST"])
@login_required
def inventory_import_csv() -> ResponseReturnValue:
	"""
	Bulk-imports devices from an uploaded CSV file into the user's inventory.
	Saves the upload to a temp file and delegates to
	InputParser.csv_to_inventory: attribute columns become variable
	attributes, credential columns become security profiles (when the
	checkbox is on), other columns are reported. No reachability check -
	Inventory shows it live. Row errors and notices are flashed.
	"""
	csv_file = request.files.get("csv_file")
	if not csv_file or not csv_file.filename:
		flash("No file selected.", "danger")
		return redirect(url_for("inventory.inventory"))

	label = request.form.get("label", "").strip() or None
	create_profiles = request.form.get("create_profiles") == "on"

	# Save upload to a temp file - csv_to_inventory takes a path, not a file object
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
			view = InventoryView(db_session, viewer())
			sys_props, user_props = view.property_defs()
			# endpoints already in use, read before the import adds rows
			in_use = view.visible_endpoints()
			report = import_csv(
				parser, tmp_path, current_user.id, db_session, label=label,
				properties=sys_props + user_props,
				create_profiles=create_profiles,
				dns_suffix=current_app.web.dns_suffix())

		devices = report.devices
		# same ip:port as an existing visible device, or twice in the file -
		# a hostname also by the address it resolved to: allowed (NAT, VRFs,
		# labs), but say so
		seen: set[tuple[str, int]] = set()
		shared: list[str] = []
		for d in devices:
			targets = {(d.ip, d.port)}
			if d.ip in report.resolved:
				targets.add((report.resolved[d.ip], d.port))
			if targets & (in_use | seen):
				shared.append(endpoint(d.ip, d.port))
			seen |= targets
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
			current_app.web.audit(AuditAction.SECURITY_PROFILE_CREATE,
			                      object_type="SecurityProfile",
			                      object_id=profile_id,
			                      object_label=profile_label,
			                      detail={"source": "csv_import"})
		if devices:
			current_app.web.audit(AuditAction.INVENTORY_IMPORT_CSV, detail={
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
	"""Link one of the user's security profiles to devices they see - their
	own and global ones (the link is theirs alone) - or unlink theirs (no
	profile_id): JSON {profile_id, device_ids}. Bad or unseen ids are
	skipped, each one logged."""
	logger = RolloutLogger(webapp=False, verbose=False,
	                       prefix="bulk_sec_assign",
	                       job_id=str(uuid.uuid4())[:8])
	data = request.get_json(silent=True)
	if not data:
		logger.notify("Bulk assign failed: invalid request", Tone.ERROR,
		              important=True)
		return err("Invalid request")

	profile_id = data.get("profile_id")
	device_ids = data.get("device_ids", [])

	if not device_ids:
		logger.notify("Bulk assign failed: no devices provided", Tone.ERROR,
		              important=True)
		return err("No devices provided")

	try:
		parsed_profile_id = uuid.UUID(str(profile_id)) if profile_id else None
	except ValueError:
		return err("Invalid profile ID", 422)
	logger.notify(
		f"Bulk security assign started: {len(device_ids)} devices → profile {profile_id or 'unassign'}",
		important=True)

	with current_app.backend.postgres.get_session() as db_session:
		view = InventoryView(db_session, viewer())
		if parsed_profile_id:
			profile = SecurityProfiles(db_session, viewer()).owned(parsed_profile_id)
			if not profile:
				logger.notify("Bulk assign failed: profile not found", Tone.ERROR,
				              important=True)
				return err("Profile not found", 404)

		assigned, skipped = 0, 0
		for device_id_str in device_ids:
			try:
				parsed_device_id = uuid.UUID(str(device_id_str))
			except (ValueError, TypeError):
				skipped += 1
				continue
			# any device the user sees: the link is the user's own
			device = view.get_visible(parsed_device_id)
			if device:
				view.link_profile(device, parsed_profile_id)
				logger.notify(f"{device.label} ({device.ip}): "
				              f"{'assigned' if parsed_profile_id else 'unassigned'}",
				              Tone.SUCCESS)
				assigned += 1
			else:
				logger.notify(f"Device {device_id_str}: not found", Tone.ERROR)
				skipped += 1

	logger.notify(
		f"Bulk security assign complete: {assigned} assigned, {skipped} skipped",
		Tone.SUCCESS if not skipped else Tone.WARNING, important=True)
	current_app.web.audit(AuditAction.INVENTORY_BULK_ASSIGN, detail={
		"count": len(device_ids),
		"profile_id": str(profile_id) if profile_id else None})
	return ok()
