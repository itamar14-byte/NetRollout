# python utilities
import json
import uuid
from itertools import groupby

# services
# flask
from flask import Blueprint, render_template, current_app, request, flash, \
	redirect, url_for, Response
from flask_login import current_user, login_required


# local modules
from src.db.tables import DeviceResult, User, Inventory
from src.core import RolloutOptions
from src.input_parser import InputParser
from src.webapp.utils import ok, err, with_form, with_json

bp = Blueprint('rollout', __name__)


##############################Route Helpers################################
def parse_commands() -> tuple | Response:
	# 3) Detect single vs. multi-platform mode early.
	# The frontend sends a JSON field "platform_commands" when multiple platforms
	# are selected, and omits it (or sends empty) for single-platform rollouts.
	raw_platform_commands = request.form.get("platform_commands", "").strip()
	if raw_platform_commands:
		# Multi-platform: parse the JSON map of platform → command text.
		try:
			platform_commands_map = json.loads(raw_platform_commands)
		except json.JSONDecodeError:
			flash("Invalid platform commands format.", "danger")
			return redirect(url_for("rollout.new_rollout"))
		# commands is unused in multi-platform mode — each platform has its own
		return None, platform_commands_map, True

	# 4) Single-platform: file upload or pasted text.
	commands_file = request.files.get("commands_file")
	manual_commands = request.form.get("manual_commands", "").strip()

	if commands_file and commands_file.filename:
		if not commands_file.filename.lower().endswith(".txt"):
			flash("Command file must be a .txt file.", "danger")
			return redirect(url_for("rollout.new_rollout"))
		try:
			commands = [
				line for raw_line in commands_file.readlines()
				if (line := raw_line.decode("utf-8").strip())
			]
		except UnicodeDecodeError:
			flash("Command file must be valid UTF-8 text.", "danger")
			return redirect(url_for("rollout.new_rollout"))
	else:
		commands = [l.strip() for l in manual_commands.splitlines() if l.strip()]

	if not commands:
		flash("Provide commands by pasting text or uploading a command file.",
		      "danger")
		return redirect(url_for("rollout.new_rollout"))

	return commands, {}, False


def load_devices(selected_ids: list) -> list | Response:
	# 6) Load the selected inventory rows belonging to the current user.
	with current_app.backend.postgres.get_session() as db_session:
		selected_rows = (
			db_session.query(Inventory)
			.filter(Inventory.user_id == current_user.id,
			        Inventory.id.in_(selected_ids))
			.all()
		)
		# Preload relationships needed for runtime device construction.
		_ = [row.security_profile for row in selected_rows]
		_ = [row.var_mappings for row in selected_rows]
		db_session.expunge_all()

	# 7) Validate the selected devices.
	if not selected_rows:
		flash("No valid devices selected.", "danger")
		return redirect(url_for("rollout.new_rollout"))

	if len(selected_rows) != len(selected_ids):
		flash("One or more selected devices were not found.", "danger")
		return redirect(url_for("rollout.new_rollout"))

	# 8) Ensure every device has a security profile.
	missing_profiles = [row.label or row.ip for row in selected_rows
	                    if not row.security_profile]
	if missing_profiles:
		flash("These devices have no security profile assigned: "
		      + ", ".join(missing_profiles), "danger")
		return redirect(url_for("rollout.new_rollout"))

	# 9) Convert ORM inventory rows into runtime Device objects.
	try:
		return InputParser.import_from_inventory(selected_rows)
	except ValueError as e:
		flash(str(e), "danger")
		return redirect(url_for("rollout.new_rollout"))


def submit_jobs(devices, commands, platform_commands_map, is_multi_platform,
                options, audit_comment) -> uuid.UUID | Response:
	# 10) Submit jobs to the orchestrator.
	if not is_multi_platform:
		return current_app.orchestrator.submit(devices, commands, options,
		                                       current_user.id, audit_comment)

	# Backend enforces multi-platform — never trust the frontend alone.
	actual_platforms = {d.device_type for d in devices}
	if len(actual_platforms) < 2:
		flash("Expected multiple platforms but only one found.", "danger")
		return redirect(url_for("rollout.new_rollout"))

	devices.sort(key=lambda d: d.device_type)
	job_id = None
	for platform, group in groupby(devices, key=lambda d: d.device_type):
		curr_commands = [l.strip() for l in
		                 platform_commands_map.get(platform, "").splitlines()
		                 if l.strip()]
		if not curr_commands:
			flash(f"No commands provided for {platform}.", "danger")
			return redirect(url_for("rollout.new_rollout"))
		first_job = current_app.orchestrator.submit(list(group), curr_commands,
		                                            options, current_user.id,
		                                            audit_comment)
		if job_id is None:
			job_id = first_job
	return job_id


##############################Routes#######################################
@bp.route("/cancel_rollout", methods=["POST"])
@login_required
@with_form("job_id")
def cancel_rollout(data):
	raw = data.get("job_id", "").strip()
	try:
		job_id = uuid.UUID(raw)
	except ValueError:
		return err("invalid job_id", 422)
	job = current_app.orchestrator.get_job(job_id)
	if not job:
		return err("job not found", 404)
	if job.user_id != current_user.id and current_user.role != "admin":
		return err("job not assigned to user", 403)
	current_app.orchestrator.cancel(job_id)
	current_app.web.audit("rollout.cancel", object_id=job_id)
	return ok("canceled")


@bp.route("/new_rollout")
@login_required
def new_rollout():
	with current_app.backend.postgres.get_session() as db_session:
		user = db_session.get(User, current_user.id)
		devices = user.inventory
		_ = [d.security_profile for d in devices]
		_ = [d.var_mappings for d in devices]
		db_session.expunge_all()

	return render_template("new_rollout.html",
	                       devices=devices,
	                       active_section="rollout"
	                       )

@bp.route("/new_start_rollout", methods=["POST"])
@login_required
def new_start_rollout():
	raw_device_ids = request.form.getlist("device_ids")
	if not raw_device_ids:
		flash("Select at least one device.", "danger")
		return redirect(url_for("rollout.new_rollout"))

	try:
		selected_ids = list({uuid.UUID(did) for did in raw_device_ids})
	except ValueError:
		flash("Invalid device selection.", "danger")
		return redirect(url_for("rollout.new_rollout"))

	result = parse_commands()
	if isinstance(result, Response):
		return result
	commands, platform_commands_map, is_multi_platform = result

	devices = load_devices(selected_ids)
	if isinstance(devices, Response):
		return devices

	options = RolloutOptions(
		verify=bool(request.form.get("_verify", "")),
		verbose=bool(request.form.get("_verbose", "")),
		webapp=True
	)
	audit_comment = request.form.get("comment", "").strip() or None

	job_id = submit_jobs(devices, commands, platform_commands_map,
	                     is_multi_platform, options, audit_comment)
	if isinstance(job_id, Response):
		return job_id

	current_app.web.audit("rollout.start", object_id=job_id,
	                      detail={"device_count": len(devices),
	                              "comment": audit_comment})
	flash(f"Rollout started for {len(devices)} "
	      f"device{'s' if len(devices) != 1 else ''}.", "success")
	return redirect(url_for("jobs.active_jobs", new=str(job_id)))


@bp.route("/rollout_stream/<uuid:job_id>")
@login_required
def rollout_stream(job_id):
	job = current_app.orchestrator.get_job(job_id)
	if not job or (
			job.user_id != current_user.id and current_user.role != "admin"):
		return Response(status=403)

	def generate():
		# Drain queue items already covered by the redis list
		snapshot = job.get_log_history()
		for msg in snapshot:
			yield f"data: {msg}\n\n"

		ps = job.get_log_queue()
		try:
			while True:
				msg = ps.get_message(timeout=0.5)
				if msg and msg["type"] == "message":
					data = msg["data"].decode()
					if data == "__done__":
						break
					yield f"data: {data}\n\n"
				elif not job.is_alive():
					break
				else:
					yield f"data: \n\n"
		finally:
			ps.close()
		yield "event: done\ndata: \n\n"

	return Response(
		generate(),
		mimetype="text/event-stream",
		headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
	)

@bp.route("/rollback/<uuid:job_id>", methods=["POST"])
@login_required
@with_json("commands")
def rollback(job_id, data):
	# Fetch compensatory commands

	# Get successful devices
	with current_app.backend.postgres.get_session() as db_session:
		result = db_session.query(DeviceResult).filter_by(
			user_id=current_user.id,
			job_id=job_id,
			status="success").all()
		successful_ips = {r.device_ip for r in result}

		rows = db_session.query(Inventory).filter(
			Inventory.user_id == current_user.id,
			Inventory.ip.in_(successful_ips)).all()
		if not rows:
			return err("No successfully configured devices found for this job.")

		_ = [row.security_profile for row in rows]
		_ = [row.var_mappings for row in rows]
		db_session.expunge_all()

	commands = [l.strip() for l in data["commands"].splitlines() if l.strip()]
	devices = InputParser.import_from_inventory(rows)
	options = RolloutOptions(
		verify=bool(data.get("verify", False)),
		verbose=bool(data.get("verbose", False)),
		webapp=True
	)
	new_job_id = current_app.orchestrator.submit(devices, commands, options,
	                                 current_user.id)
	current_app.web.audit("rollout.rollback", object_id=job_id,
	      detail={"new_job_id": str(new_job_id), "device_count": len(devices)})
	return ok(job_id=str(new_job_id))
