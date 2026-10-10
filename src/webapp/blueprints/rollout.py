"""New rollout: pick devices, give the commands (one set, or one per
platform), start; the live log's stream, cancel, and a rollback of a job's
successful devices."""
import json
import uuid
from collections.abc import Callable, Iterator
from itertools import groupby
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, render_template, request, flash, redirect, url_for, Response
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required

from src.audit import AuditAction
from src.accounts.users import Viewer
from src.db.tables import Inventory, JobMetadata, User
from src.inventory import InventoryView, partition_devices
from src.jobs import QUEUED_LINE, Draining
from src.results import JobResults
from src.rollout.engine import Device, RolloutOptions, missing_value
from src.rollout.inputs import InputParser
from src.webapp.app import current_app
from src.webapp.http import Refused, ok, err, with_form, with_json, Caller, viewer

bp = Blueprint('rollout', __name__, url_prefix='/rollout')


def parse_commands() -> tuple[list[str] | None, dict[str, str], bool]:
	"""The start form's commands. The page sends "platform_commands" (JSON:
	platform → command text) when the devices are of several platforms;
	else a commands file or pasted text.

	:returns: (commands - None for several platforms, platform → command
	 text, several platforms)
	:raises Refused: no usable commands - the reason"""
	raw_platform_commands = request.form.get("platform_commands", "").strip()
	if raw_platform_commands:
		# Multi-platform: parse the JSON map of the platform → command text.
		try:
			platform_commands_map = json.loads(raw_platform_commands)
		except json.JSONDecodeError:
			raise Refused("Invalid platform commands format.") from None
		# commands is unused in multi-platform mode — each platform has its own
		return None, platform_commands_map, True

	# 4) Single-platform: file upload or pasted text.
	commands_file = request.files.get("commands_file")
	manual_commands = request.form.get("manual_commands", "").strip()

	if commands_file and commands_file.filename:
		if not commands_file.filename.lower().endswith(".txt"):
			raise Refused("Command file must be a .txt file.")
		try:
			commands = [
				line for raw_line in commands_file.readlines()
				if (line := raw_line.decode("utf-8").strip())
			]
		except UnicodeDecodeError:
			raise Refused("Command file must be valid UTF-8 text.") from None
	else:
		commands = [l.strip() for l in manual_commands.splitlines() if l.strip()]

	if not commands:
		raise Refused("Provide commands by pasting text or uploading a command file.")

	return commands, {}, False


def load_devices(selected_ids: list[uuid.UUID]) -> list[Device]:
	"""The selected devices the user may see (own and global), as rollout
	targets with their credentials.

	:returns: the devices
	:raises Refused: none found, some missing, one without a security
	 profile"""
	with current_app.backend.postgres.get_session() as db_session:
		view = InventoryView(db_session, viewer())
		# relationships preloaded for runtime device construction
		selected_rows = view.visible_ids(selected_ids)
		values = view.attributes(selected_rows)
		db_session.expunge_all()

	# 7) Validate the selected devices.
	if not selected_rows:
		raise Refused("No valid devices selected.")

	if len(selected_rows) != len(selected_ids):
		raise Refused("One or more selected devices were not found.")

	# 8) Ensure every device has a security profile.
	missing_profiles = [row.label or row.ip for row in selected_rows
	                    if not row.security_profile]
	if missing_profiles:
		raise Refused("These devices have no security profile assigned: "
		              + ", ".join(missing_profiles))

	# 9) Convert ORM inventory rows into runtime Device objects.
	try:
		return InputParser.import_from_inventory(selected_rows, current_user.id,
		                                         values)
	except ValueError as e:
		raise Refused(str(e)) from None


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


def unresolved_message(devices: list[Device], commands_of: Callable[[Device], list[str]]) -> str | None:
	"""The refusal naming every device whose commands keep a token that can't
	be filled in (Device.unresolved: no mapping on the device, or no value) -
	checked before a rollout is queued, as reachability is.

	:param commands_of: the commands a device would get (its platform's)
	:returns: the message; None when every token resolves"""
	blocked = [(d, problems) for d in devices if (problems := d.unresolved(commands_of(d)))]
	if not blocked:
		return None
	names = "; ".join(f"{d.label or d.ip} ({d.endpoint}): {', '.join(problems)}"
	                  for d, problems in blocked)
	return (f"Tokens that can't be filled in — rollout blocked: {names}. Fix the "
	        f"commands or the devices' attributes and mappings, or leave those "
	        f"devices out.")


def platform_lines(text: str) -> list[str]:
	""":returns: a platform's command text as the commands it runs (blank lines out)"""
	return [line.strip() for line in text.splitlines() if line.strip()]


def submit_jobs(devices: list[Device], commands: list[str] | None,
                platform_commands_map: dict[str, str], is_multi_platform: bool,
                options: RolloutOptions,
                audit_comment: str | None) -> uuid.UUID:
	"""Queue the rollout: one job, or one per platform with its own commands -
	all of them or none: every platform's commands are checked before the
	first job is queued, and if queuing one fails (NetRollout starts
	stopping or pauses for a database move between two platforms, a service
	fails), the jobs already queued are cancelled.

	:returns: the (first) job's id
	:raises Refused: the platforms or their commands don't fit - the reason
	:raises Draining: NetRollout is stopping, or paused for a database move"""
	if not is_multi_platform:
		assert commands is not None   # parse_commands gives them for one platform
		return current_app.orchestrator.submit(devices, commands, options,
		                                       current_user.id, audit_comment)

	# Backend enforces multi-platform — never trust the frontend alone.
	actual_platforms = {d.device_type for d in devices}
	if len(actual_platforms) < 2:
		raise Refused("Expected multiple platforms but only one found.")

	devices.sort(key=lambda d: d.device_type)
	jobs: list[tuple[list[Device], list[str]]] = []
	for platform, group in groupby(devices, key=lambda d: d.device_type):
		curr_commands = platform_lines(platform_commands_map.get(platform, ""))
		if not curr_commands:
			raise Refused(f"No commands provided for {platform}.")
		jobs.append((list(group), curr_commands))

	queued: list[uuid.UUID] = []
	try:
		for group_devices, group_commands in jobs:
			queued.append(current_app.orchestrator.submit(
				group_devices, group_commands, options, current_user.id,
				audit_comment))
	except Exception:   # stopping / paused, or a service failing: none or all
		for job_id in queued:
			current_app.orchestrator.cancel(job_id)
		raise
	return queued[0]


@bp.route("/cancel", methods=["POST"])
@login_required
@with_form("job_id")
def cancel_rollout(data: Any) -> ResponseReturnValue:
	"""Cancel a running or queued rollout - the user's own, or any for an
	admin. Someone else's job is answered as one that doesn't exist (404 "job
	not found"), so its existence isn't revealed.

	:returns: JSON for a script (XHR / JSON); a page's form gets the outcome
	 flashed and is sent back to the page it came from (else Active Jobs)"""
	scripted = Caller.SCRIPT.wants_json()

	def refused(message: str, code: int) -> ResponseReturnValue:
		if scripted:
			return err(message, code)
		flash(f"The rollout could not be cancelled: {message}.", "danger")
		return redirect(_back())

	raw = data.get("job_id", "").strip()
	try:
		job_id = uuid.UUID(raw)
	except ValueError:
		return refused("invalid job_id", 422)
	job = current_app.orchestrator.get_job(job_id)
	if not job or (job.user_id != current_user.id and not viewer().is_admin):
		return refused("job not found", 404)
	queued = job.started_at is None        # it ends at once, nothing pushed
	current_app.orchestrator.cancel(job_id)
	current_app.web.audit(AuditAction.ROLLOUT_CANCEL, object_id=job_id)
	if scripted:
		return ok("canceled")
	flash("Queued rollout cancelled - it never started." if queued else
	      "Rollout cancelled - devices it has not reached are skipped.", "success")
	return redirect(_back())


def _back() -> str:
	""":returns: where a page's form goes back to - the referring page's path
	 on this site (not its query: Active Jobs' auto-reload adds ?_bg=1,
	 which marks a request as background), else Active Jobs"""
	referrer = urlsplit(request.referrer or "")
	if referrer.netloc == request.host and referrer.path.startswith("/"):
		return referrer.path
	return url_for("jobs.active_jobs")


@bp.route("/new")
@login_required
def new_rollout() -> str:
	"""The new rollout page: the devices the user may roll out to."""
	with current_app.backend.postgres.get_session() as db_session:
		view = InventoryView(db_session, viewer())
		devices = view.visible()
		tokens = token_states(devices, view.attributes(devices), current_user.id)
		db_session.expunge_all()
	global_devices, my_devices = partition_devices(devices)

	return render_template("new_rollout.html",
	                       global_devices=global_devices,
	                       my_devices=my_devices,
	                       token_states=tokens,
	                       active_section="rollout"
	                       )


def token_states(devices: list[Inventory], values: dict[uuid.UUID, dict[str, Any]],
                 user_id: uuid.UUID) -> dict[str, dict[str, str | None]]:
	"""What the New Rollout page needs to block a token that can't be filled
	in before the launch does (Device.unresolved): per device, the tokens the
	user bound on it and why each can't substitute.

	:param devices: rows with their mappings loaded (every user's)
	:param values: each device's attribute values as the user sees them
	 (InventoryView.attributes - never another user's)
	:returns: {device id: {token: None when it resolves, else the reason}};
	 devices without the user's mappings left out"""
	states: dict[str, dict[str, str | None]] = {}
	for device in devices:
		own = {m.token: missing_value(values.get(device.id), m.property_name, m.index)
		       for m in device.var_mappings if m.user_id == user_id}
		if own:
			states[str(device.id)] = own
	return states

def start_rollout() -> tuple[uuid.UUID, list[Device], str | None]:
	"""The start form's checks, in order, then the rollout queued
	(submit_jobs).

	:returns: the (first) job's id, the devices, the comment
	:raises Refused: a check refused it - the reason
	:raises Draining: NetRollout is stopping, or paused for a database move"""
	# before the reachability checks: stopping, or paused for a database move
	if refused := current_app.orchestrator.refusal():
		raise Refused(refused)
	raw_device_ids = request.form.getlist("device_ids")
	if not raw_device_ids:
		raise Refused("Select at least one device.")

	try:
		selected_ids = list({uuid.UUID(did) for did in raw_device_ids})
	except ValueError:
		raise Refused("Invalid device selection.") from None

	commands, platform_commands_map, is_multi_platform = parse_commands()
	devices = load_devices(selected_ids)

	if duplicates := duplicate_targets(devices):
		raise Refused("The same target was selected more than once — select only one "
		              "of: " + "; ".join(f"{' / '.join(labels)} ({ep})"
		                                 for ep, labels in duplicates.items()))

	if unreachable := unreachable_devices(devices):
		raise Refused(unreachable_message(unreachable))

	if blocked := unresolved_message(devices, lambda d: platform_lines(
			platform_commands_map.get(d.device_type, "")) if is_multi_platform
			else commands or []):
		raise Refused(blocked)

	options = RolloutOptions(
		verify=bool(request.form.get("_verify", "")),
		verbose=bool(request.form.get("_verbose", "")),
		webapp=True,
		max_workers=current_app.backend.settings.get("device_parallelism")
	)
	audit_comment = request.form.get("comment", "").strip() or None
	job_id = submit_jobs(devices, commands, platform_commands_map,
	                     is_multi_platform, options, audit_comment)
	return job_id, devices, audit_comment


@bp.route("/start", methods=["POST"])
@login_required
def new_start_rollout() -> ResponseReturnValue:
	"""Start a rollout from the page's form: the devices (none twice, all
	reachable), the commands, verify / verbose, a comment. Audited
	(rollout.start).

	:returns: the way to Active Jobs; or back to the form, the reason flashed"""
	try:
		job_id, devices, audit_comment = start_rollout()
	except (Refused, Draining) as e:   # Draining: stopping, or paused for a database move
		flash(str(e), "danger")
		return redirect(url_for("rollout.new_rollout"))

	current_app.web.audit(AuditAction.ROLLOUT_START, object_id=job_id,
	                      detail={"device_count": len(devices),
	                              "comment": audit_comment})
	flash(f"Rollout started for {len(devices)} "
	      f"device{'s' if len(devices) != 1 else ''}.", "success")
	return redirect(url_for("jobs.active_jobs", new=str(job_id)))


@bp.route("/stream/<uuid:job_id>")
@login_required
def rollout_stream(job_id: uuid.UUID) -> Response:
	"""The live log (Server-Sent Events): for a queued job a "Queued" line,
	what's logged so far, then each new line, a heartbeat comment every
	0.5 s, and "done" at the end. The job's owner or an admin. A job that has
	already ended (or never ran here) gets just "done" - the page then shows
	its outcome; 404 when nothing is known of it, and when it's someone else's
	(the same answer, so its existence isn't revealed)."""
	orchestrator = current_app.orchestrator   # the generator runs after the request
	job = orchestrator.get_job(job_id)
	if job is None:
		with current_app.backend.postgres.get_session() as db_session:
			owner = db_session.query(JobMetadata.user_id).filter_by(job_id=job_id).scalar()
		if owner is None or (owner != current_user.id and not viewer().is_admin):
			return Response(status=404)
		return _event_stream(iter([DONE_EVENT]))
	if job.user_id != current_user.id and not viewer().is_admin:
		return Response(status=404)
	followed = job

	def over() -> bool:
		return followed.is_over() or orchestrator.get_job(job_id) is None

	def generate() -> Iterator[str]:
		if followed.started_at is None:
			yield sse_data(QUEUED_LINE)
		for line in followed.follow_log(over):
			yield HEARTBEAT if line is None else sse_data(line)
		yield DONE_EVENT

	return _event_stream(generate())


# a comment line: keeps the connection (and nginx) alive, not a message
HEARTBEAT = ": hb\n\n"


def sse_data(text: str) -> str:
	""":returns: one SSE message carrying `text` whole - a line of its own per
	 line (a bare "\\r" would end an SSE line too)"""
	lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
	return "".join(f"data: {line}\n" for line in lines) + "\n"


DONE_EVENT = "event: done\n" + sse_data("")


def _event_stream(events: Iterator[str]) -> Response:
	""":returns: the SSE response, unbuffered by nginx"""
	return Response(events, mimetype="text/event-stream",
	                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@bp.route("/rollback/<uuid:job_id>", methods=["POST"])
@login_required
@with_json("commands")
def rollback(job_id: uuid.UUID, data: dict[str, Any]) -> ResponseReturnValue:
	"""A new rollout of the given commands (JSON {commands, verify?,
	verbose?}) to the devices a finished job configured successfully - the
	user's own job, or anyone's for an admin. The devices are the job
	owner's: matched by ip:port among what the owner may see (their own
	entry preferred), their variables resolved with the owner's mappings, so
	the rollback targets what the job configured; the new job is the
	signed-in user's. Audited (rollout.rollback, with the job's owner).

	:returns: {"status": "ok", job_id}; 404 when the job has no results (yet)
	 or isn't the user's; or another error"""
	with current_app.backend.postgres.get_session() as db_session:
		history = JobResults(db_session, viewer())
		owner_id = history.get_accessible(job_id)
		if owner_id is None:
			return err("Not found", 404)
		owner = db_session.get(User, owner_id)
		owner_name = owner.username if owner else None
		# the owner's devices and values, as they see them
		owner_view = InventoryView(db_session, Viewer.of(owner) if owner else Viewer(owner_id))
		successful = history.successful_endpoints(job_id, owner_id)

		candidates = owner_view.visible(ips={ip for ip, _ in successful})
		# Match on ip:port (the target), not IP alone — devices behind one
		# NAT address differ by port. The owner's own entry and a global
		# entry can still describe the same target: keep one per ip:port,
		# preferring the owner's own, so the box isn't pushed twice.
		by_target: dict[tuple[str, int], Inventory] = {}
		for row in candidates:
			target = (row.ip, row.port)
			if target not in successful:
				continue
			if target not in by_target or row.user_id == owner_id:
				by_target[target] = row
		rows = list(by_target.values())
		if not rows:
			return err("No successfully configured devices found for this job.")

		values = owner_view.attributes(rows)   # the owner's values (relationships preloaded)
		db_session.expunge_all()

	commands = [l.strip() for l in data["commands"].splitlines() if l.strip()]
	try:
		devices = InputParser.import_from_inventory(rows, owner_id, values)
	except ValueError as e:              # a device without a security profile
		return err(f"Can't roll back: {e}.", 409)
	if unreachable := unreachable_devices(devices):
		return err(unreachable_message(unreachable), 409)
	if blocked := unresolved_message(devices, lambda d: commands):
		return err(blocked, 409)
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
	current_app.web.audit(AuditAction.ROLLOUT_ROLLBACK, object_id=job_id,
	                      detail={"new_job_id": str(new_job_id),
	                              "device_count": len(devices),
	                              "job_owner_id": str(owner_id),
	                              "job_owner": owner_name})
	return ok(job_id=str(new_job_id))
