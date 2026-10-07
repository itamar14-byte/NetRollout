"""New rollout: pick devices, give the commands (one set, or one per
platform), start; the live log's stream, cancel, and a rollback of a job's
successful devices."""
import json
import uuid
from collections.abc import Iterator
from itertools import groupby
from typing import Any, cast

from flask import Blueprint, render_template, request, flash, redirect, url_for, Response
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from werkzeug.wrappers import Response as BaseResponse

from src.db.tables import DeviceResult, Inventory
from src.orchestration import Draining
from src.rollout.engine import Device, RolloutOptions
from src.rollout.inputs import InputParser
from src.webapp.flask_app import current_app
from src.webapp.utils import (ok, err, with_form, with_json,
                              visible_devices_clause, query_visible_devices,
                              partition_devices)

bp = Blueprint('rollout', __name__, url_prefix='/rollout')


##############################Route Helpers################################
def parse_commands() -> tuple[list[str] | None, dict[str, str], bool] | BaseResponse:
	"""The start form's commands. The page sends "platform_commands" (JSON:
	platform → command text) when the devices are of several platforms;
	else a commands file or pasted text.

	:returns: (commands - None for several platforms, platform → command
	 text, several platforms); or the way back to the form, the reason flashed"""
	raw_platform_commands = request.form.get("platform_commands", "").strip()
	if raw_platform_commands:
		# Multi-platform: parse the JSON map of the platform → command text.
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


def load_devices(selected_ids: list[uuid.UUID]) -> list[Device] | BaseResponse:
	"""The selected devices the user may see (own and global), as rollout
	targets with their credentials.

	:returns: the devices; or the way back to the form, the reason flashed
	 (none found, some missing, one without a security profile)"""
	with current_app.backend.postgres.get_session() as db_session:
		selected_rows = (
			db_session.query(Inventory)
			.filter(visible_devices_clause(current_user.id),
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
		return InputParser.import_from_inventory(selected_rows, current_user.id)
	except ValueError as e:
		flash(str(e), "danger")
		return redirect(url_for("rollout.new_rollout"))


def duplicate_targets(devices: list[Device]) -> dict[str, list[str]]:
	"""The same ip:port selected twice is the same box twice (e.g. an own
	entry and a global entry for one router): the config would be pushed
	twice. The same IP on different ports is fine — targets behind NAT.

	:returns: ip:port → the labels selected for it, where more than one"""
	by_endpoint: dict[str, list[str]] = {}
	for d in devices:
		by_endpoint.setdefault(d.endpoint, []).append(d.label or d.ip)
	return {ep: labels for ep, labels in by_endpoint.items() if len(labels) > 1}


def unreachable_devices(devices: list[Device]) -> list[Device]:
	"""The devices NetRollout can't reach now. A cached "reachable" is
	trusted; a cached failure is probed again, so a device that just came
	back isn't blocked by a stale result."""
	results = current_app.web.reachability.check(
		[(d.ip, d.port) for d in devices], recheck_unreachable=True)
	return [d for d in devices
	        if not results[(d.ip, int(d.port))]["reachable"]]


def unreachable_message(devices: list[Device]) -> str:
	""":returns: the refusal naming the unreachable devices"""
	names = ", ".join(f"{d.label or d.ip} ({d.endpoint})" for d in devices)
	return (f"Not reachable from NetRollout — rollout blocked: {names}. "
	        f"Recheck once they're back online.")


def submit_jobs(devices: list[Device], commands: list[str] | None,
                platform_commands_map: dict[str, str], is_multi_platform: bool,
                options: RolloutOptions,
                audit_comment: str | None) -> uuid.UUID | BaseResponse:
	"""Queue the rollout: one job, or one per platform with its own commands -
	all of them or none: every platform's commands are checked before the
	first job is queued, and if NetRollout starts stopping (or pauses for a
	database move) between two platforms, the jobs already queued are
	cancelled.

	:returns: the (first) job's id; or the way back to the form, the reason
	 flashed
	:raises Draining: NetRollout is stopping, or paused for a database move"""
	if not is_multi_platform:
		assert commands is not None   # parse_commands gives them for one platform
		return current_app.orchestrator.submit(devices, commands, options,
		                                       current_user.id, audit_comment)

	# Backend enforces multi-platform — never trust the frontend alone.
	actual_platforms = {d.device_type for d in devices}
	if len(actual_platforms) < 2:
		flash("Expected multiple platforms but only one found.", "danger")
		return redirect(url_for("rollout.new_rollout"))

	devices.sort(key=lambda d: d.device_type)
	jobs: list[tuple[list[Device], list[str]]] = []
	for platform, group in groupby(devices, key=lambda d: d.device_type):
		curr_commands = [l.strip() for l in
		                 platform_commands_map.get(platform, "").splitlines()
		                 if l.strip()]
		if not curr_commands:
			flash(f"No commands provided for {platform}.", "danger")
			return redirect(url_for("rollout.new_rollout"))
		jobs.append((list(group), curr_commands))

	queued: list[uuid.UUID] = []
	try:
		for group_devices, group_commands in jobs:
			queued.append(current_app.orchestrator.submit(
				group_devices, group_commands, options, current_user.id,
				audit_comment))
	except Draining:
		for job_id in queued:
			current_app.orchestrator.cancel(job_id)
		raise
	return queued[0]


##############################Routes#######################################
@bp.route("/cancel", methods=["POST"])
@login_required
@with_form("job_id")
def cancel_rollout(data: Any) -> ResponseReturnValue:
	"""Cancel a running or queued rollout - the user's own, or any for an
	admin."""
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


@bp.route("/new")
@login_required
def new_rollout() -> str:
	"""The new rollout page: the devices the user may roll out to."""
	with current_app.backend.postgres.get_session() as db_session:
		devices = query_visible_devices(db_session, current_user.id)
		db_session.expunge_all()
	global_devices, my_devices = partition_devices(devices)

	return render_template("new_rollout.html",
	                       global_devices=global_devices,
	                       my_devices=my_devices,
	                       active_section="rollout"
	                       )

@bp.route("/start", methods=["POST"])
@login_required
def new_start_rollout() -> ResponseReturnValue:
	"""Start a rollout from the page's form: the devices (none twice, all
	reachable), the commands, verify / verbose, a comment. Audited
	(rollout.start).

	:returns: the way to Active Jobs; or back to the form, the reason flashed"""
	# before the reachability checks: stopping, or paused for a database move
	if refused := current_app.orchestrator.refusal():
		flash(refused, "danger")
		return redirect(url_for("rollout.new_rollout"))
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
	if isinstance(result, BaseResponse):
		return result
	commands, platform_commands_map, is_multi_platform = result

	devices = load_devices(selected_ids)
	if isinstance(devices, BaseResponse):
		return devices

	if duplicates := duplicate_targets(devices):
		flash("The same target was selected more than once — select only one "
		      "of: " + "; ".join(f"{' / '.join(labels)} ({ep})"
		                         for ep, labels in duplicates.items()),
		      "danger")
		return redirect(url_for("rollout.new_rollout"))

	if unreachable := unreachable_devices(devices):
		flash(unreachable_message(unreachable), "danger")
		return redirect(url_for("rollout.new_rollout"))

	options = RolloutOptions(
		verify=bool(request.form.get("_verify", "")),
		verbose=bool(request.form.get("_verbose", "")),
		webapp=True,
		max_workers=current_app.backend.settings.get("device_parallelism")
	)
	audit_comment = request.form.get("comment", "").strip() or None

	try:
		job_id = submit_jobs(devices, commands, platform_commands_map,
		                     is_multi_platform, options, audit_comment)
	except Draining as e:   # stopping, or paused for a database move
		flash(str(e), "danger")
		return redirect(url_for("rollout.new_rollout"))
	if isinstance(job_id, BaseResponse):
		return job_id

	current_app.web.audit("rollout.start", object_id=job_id,
	                      detail={"device_count": len(devices),
	                              "comment": audit_comment})
	flash(f"Rollout started for {len(devices)} "
	      f"device{'s' if len(devices) != 1 else ''}.", "success")
	return redirect(url_for("jobs.active_jobs", new=str(job_id)))


@bp.route("/stream/<uuid:job_id>")
@login_required
def rollout_stream(job_id: uuid.UUID) -> Response:
	"""The live log (Server-Sent Events): what's logged so far, then each new
	line, a heartbeat every 0.5 s, and "done" at the end. The job's owner or
	an admin; the job must be running in this process."""
	job = current_app.orchestrator.get_job(job_id)
	if not job or (
			job.user_id != current_user.id and current_user.role != "admin"):
		return Response(status=403)

	def generate() -> Iterator[str]:
		snapshot = job.get_log_history()
		for line in snapshot:
			yield f"data: {line}\n\n"

		ps = job.get_log_queue()
		try:
			while True:
				# redis-py: a dict per message (its stubs say otherwise)
				msg = cast(dict[str, Any] | None, ps.get_message(timeout=0.5))
				if msg and msg["type"] == "message":
					data = cast(bytes, msg["data"]).decode()
					if data == "__done__":
						break
					yield f"data: {data}\n\n"
				elif not job.is_alive():
					break
				else:
					yield "data: \n\n"
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
def rollback(job_id: uuid.UUID, data: dict[str, Any]) -> ResponseReturnValue:
	"""A new rollout of the given commands (JSON {commands, verify?,
	verbose?}) to the devices one of the user's jobs configured successfully
	- matched by ip:port, the user's own entry preferred. Audited
	(rollout.rollback).

	:returns: {"status": "ok", job_id} or an error"""
	with current_app.backend.postgres.get_session() as db_session:
		result = db_session.query(DeviceResult).filter_by(
			user_id=current_user.id,
			job_id=job_id,
			status="success").all()
		successful = {(r.device_ip, r.device_port) for r in result}

		candidates = db_session.query(Inventory).filter(
			visible_devices_clause(current_user.id),
			Inventory.ip.in_({ip for ip, _ in successful})).all()
		# Match on ip:port (the target), not IP alone — devices behind one
		# NAT address differ by port. A user's own entry and a global entry
		# can still describe the same target: keep one per ip:port,
		# preferring the user's own, so the box isn't pushed twice.
		by_target: dict[tuple[str, int], Inventory] = {}
		for row in candidates:
			target = (row.ip, row.port)
			if target not in successful:
				continue
			if target not in by_target or row.user_id == current_user.id:
				by_target[target] = row
		rows = list(by_target.values())
		if not rows:
			return err("No successfully configured devices found for this job.")

		_ = [row.security_profile for row in rows]
		_ = [row.var_mappings for row in rows]
		db_session.expunge_all()

	commands = [l.strip() for l in data["commands"].splitlines() if l.strip()]
	try:
		devices = InputParser.import_from_inventory(rows, current_user.id)
	except ValueError as e:              # a device without a security profile
		return err(f"Can't roll back: {e}.", 409)
	if unreachable := unreachable_devices(devices):
		return err(unreachable_message(unreachable), 409)
	options = RolloutOptions(
		verify=bool(data.get("verify", False)),
		verbose=bool(data.get("verbose", False)),
		webapp=True,
		max_workers=current_app.backend.settings.get("device_parallelism")
	)
	try:
		new_job_id = current_app.orchestrator.submit(devices, commands,
		                                              options, current_user.id)
	except Draining as e:
		return err(str(e), 503)
	current_app.web.audit("rollout.rollback", object_id=job_id,
	      detail={"new_job_id": str(new_job_id), "device_count": len(devices)})
	return ok(job_id=str(new_job_id))
