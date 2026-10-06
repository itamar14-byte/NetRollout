# python utilities
import glob
import os
import uuid
from datetime import datetime, timedelta
from itertools import groupby

# services
# flask
from flask import (Blueprint, render_template, current_app, request, send_file,
                   Response, url_for)
from flask_login import current_user, login_required

# local modules
from src import runtime
from src.platforms import PLATFORMS, verify_commands
from src.db.tables import DeviceResult, JobMetadata, User, Inventory
from src.job_store import JobStore
from src.webapp.utils import ok, err, build_kpi, visible_devices_clause

bp = Blueprint('jobs', __name__)


##############################Route Helpers################################
def job_status(rows: list[DeviceResult]) -> str:
	statuses = {r.status for r in rows}
	if "cancelled" in statuses:
		return "cancelled"
	if all(r.status == "failed" for r in rows):
		return "failed"
	if any(r.status in ("failed", "partial") for r in rows):
		return "partial"
	return "success"


def visible_label_map(db_session, user_id):
	# ip -> label over the user's own and global devices. Own rows are applied
	# last so they win when a local device shares an IP with a global one.
	rows = db_session.query(Inventory.ip, Inventory.label, Inventory.user_id)\
		.filter(visible_devices_clause(user_id)).all()
	rows.sort(key=lambda r: r.user_id == user_id)
	return {r.ip: r.label for r in rows}


def visible_endpoint_labels(db_session, user_id):
	# (ip, port) -> label for per-device display: the IP alone is ambiguous
	# when several devices share it (NAT / port forwarding). Own rows win.
	rows = db_session.query(Inventory.ip, Inventory.port, Inventory.label,
	                        Inventory.user_id)\
		.filter(visible_devices_clause(user_id)).all()
	rows.sort(key=lambda r: r.user_id == user_id)
	return {(r.ip, r.port): r.label for r in rows}


def user_owns_job(job_id, user_id):
	with current_app.backend.postgres.get_session() as db_session:
		return bool(db_session.query(DeviceResult).filter_by(
			job_id=job_id, user_id=user_id).first())


def load_dashboard_data(user_id, kpi_user_id, is_admin):
	with current_app.backend.postgres.get_session() as db_session:
		# Current user's dashboard content (always own data)
		user = db_session.get(User, user_id)
		inventory_count = len(user.inventory)
		profile_count = len(user.security_profiles)
		mapping_count = len(user.variable_mappings)
		jobs_results = user.results
		inv_label_map = visible_label_map(db_session, user_id)

		# KPI data — scoped to kpi_user_id (may differ from current user for admin)
		cutoff = datetime.now() - timedelta(days=30)
		if kpi_user_id == user_id:
			kpi_results_30d = [r for r in jobs_results if
			                   r.started_at >= cutoff]
			kpi_label_map = inv_label_map
		else:
			kpi_results_30d = db_session.query(DeviceResult).filter(
				DeviceResult.user_id == kpi_user_id,
				DeviceResult.started_at >= cutoff,
			).all()
			kpi_label_map = visible_label_map(db_session, kpi_user_id)

		users = db_session.query(User).order_by(User.username).all() \
			if is_admin else []
		db_session.expunge_all()

	return {
		"inventory_count": inventory_count,
		"profile_count": profile_count,
		"mapping_count": mapping_count,
		"jobs_results": jobs_results,
		"kpi_results_30d": kpi_results_30d,
		"kpi_label_map": kpi_label_map,
		"users": users,
	}


def build_job_summaries(results):
	sorted_results = sorted(results, key=lambda x: x.job_id)
	summaries = []
	for job_id, rows in groupby(sorted_results, key=lambda x: x.job_id):
		rows = list(rows)
		summaries.append({
			"job_id": job_id,
			"completed_at": max(r.completed_at for r in rows),
			"device_count": len(rows),
			"commands_sent": rows[0].commands_sent,
			"status": job_status(rows),
			"action_needed": any(r.action_needed for r in rows),
		})
	summaries.sort(key=lambda x: x["completed_at"], reverse=True)
	return summaries


def get_active_job(user_id):
	job_ids = JobStore(current_app.backend.redis).job_ids(user_id)
	return next(
		(j for jid in job_ids
		 if (j := current_app.orchestrator.get_job(
			uuid.UUID(jid))) and j.is_alive()),
		None
	)


def config_expired(row: DeviceResult, snapshot_days: int) -> bool:
	# A snapshot is stored only when verify found a mismatch; past the
	# snapshot retention window, such rows have had it cleared by the nightly clean-up
	verify_mismatch = (row.commands_verified is not None
	                   and row.commands_verified < row.commands_sent)
	too_old = row.completed_at < datetime.now() - timedelta(
		days=snapshot_days)
	return verify_mismatch and row.fetched_config is None and too_old


def build_jobs(result_rows, metadata_by_job, endpoint_labels, snapshot_days,
               job_owner=None):
	sorted_rows = sorted(result_rows, key=lambda x: x.job_id)
	out = []
	for job_id, rows in groupby(sorted_rows, key=lambda x: x.job_id):
		rows = list(rows)
		meta = metadata_by_job.get(job_id)
		log_matches = glob.glob(
			os.path.join(runtime.logs_dir(), f"rollout_*_{job_id}.log"))
		entry = {
			"job_id": str(job_id),
			"has_log": bool(log_matches),
			"started_at": min(r.started_at for r in rows),
			"completed_at": max(r.completed_at for r in rows),
			"device_count": len(rows),
			"commands_sent": rows[0].commands_sent,
			"status": job_status(rows),
			"action_needed": any(r.action_needed for r in rows),
			"comment": meta.comment if meta else None,
			"commands": meta.commands if meta else [],
			"devices": [
				{
					"ip": r.device_ip,
					"port": r.device_port,
					"label": endpoint_labels.get(
						(r.device_ip, r.device_port),
						# unlabelled: show the port unless it's plain SSH, so
						# devices sharing an IP stay distinguishable
						r.device_ip if r.device_port == 22
						else f"{r.device_ip}:{r.device_port}"),
					"device_type": r.device_type,
					"status": r.status,
					"action_needed": r.action_needed,
					"commands_sent": r.commands_sent,
					"commands_verified": r.commands_verified,
					# flags only — the config itself is fetched on demand by
					# config_diff, never shipped with the page
					"has_config": r.fetched_config is not None,
					"config_expired": config_expired(r, snapshot_days)
				}
				for r in rows
			]
		}
		if job_owner is not None:
			entry["job_owner"] = job_owner
		out.append(entry)
	return out


def build_job_dict(job_id, usernames):
	meta = JobStore(current_app.backend.redis).meta(job_id)
	job = current_app.orchestrator.get_job(uuid.UUID(job_id))
	# Not in memory (e.g. after a restart): Redis hash values are strings,
	# and the page sums device counts — always return an int
	try:
		stored_count = int(meta.get("device_count", 0))
	except ValueError:
		stored_count = 0
	return {
		"id": job_id,
		"status": meta.get("status", "unknown"),
		"created_at": meta.get("created_at", ""),
		"device_count": job.get_device_count() if job else stored_count,
		"started_at": job.started_at.strftime(
			"%H:%M:%S") if job and job.started_at else "—",
		"started_at_iso": job.started_at.isoformat() if job and job.started_at else "",
		"owner": usernames.get(meta.get("user_id", ""), "unknown")
	}

##############################Routes################################
@bp.route("/dashboard")
@login_required
def dashboard():
	# ── Admin KPI scope ───────────────────────────────────────────────────────
	is_admin = current_user.role == "admin"
	selected_user = "me"
	kpi_user_id = current_user.id
	if is_admin:
		param = request.args.get("user", "me").strip()
		if param != "me":
			try:
				kpi_user_id = uuid.UUID(param)
				selected_user = param
			except ValueError:
				pass

	data = load_dashboard_data(current_user.id, kpi_user_id, is_admin)
	job_summaries = build_job_summaries(data["jobs_results"])
	recent_jobs = job_summaries[:5]
	total_rollouts = len(job_summaries)
	last_status = job_summaries[0]["status"] if job_summaries else None

	# ── 30-day KPI strip (scoped) ─────────────────────────────────────────────
	kpi = build_kpi(data["kpi_results_30d"], data["kpi_label_map"])

	# ── Active job (always own) ───────────────────────────────────────────────
	active_job = get_active_job(current_user.id)
	active_job_data = None
	if active_job:
		active_job_data = {
			"job_id": str(active_job.job_id),
			"device_count": active_job.get_device_count(),
			"started_at": active_job.started_at.strftime("%H:%M:%S"),
			"started_at_iso": active_job.started_at.isoformat(),
		}

	users = data["users"]
	selected_username = next(
		(u.username for u in users if str(u.id) == selected_user), selected_user
	) if selected_user != "me" else "me"

	return render_template("dashboard.html",
	                       active_section="dashboard",
	                       active_job=active_job_data,
	                       recent_jobs=recent_jobs,
	                       inventory_count=data["inventory_count"],
	                       profile_count=data["profile_count"],
	                       mapping_count=data["mapping_count"],
	                       total_rollouts=total_rollouts,
	                       last_status=last_status,
	                       kpi=kpi,
	                       users=users,
	                       selected_user=selected_user,
	                       selected_username=selected_username)


@bp.route("/active_jobs")
@login_required
def active_jobs():
	is_admin = current_user.role == "admin"
	store = JobStore(current_app.backend.redis)
	with current_app.backend.postgres.get_session() as db_session:
		if is_admin:
			job_ids = store.job_ids()
			usernames = {str(u.id): u.username for u in
			             db_session.query(User).all()}
		else:
			job_ids = store.job_ids(current_user.id)
			usernames = {}
		db_session.expunge_all()

	if is_admin:
		all_jobs = [build_job_dict(jid, usernames) for jid in job_ids]
		jobs = [j for j in all_jobs if j["owner"] == current_user.username]
		other_jobs = [j for j in all_jobs if
		              j["owner"] != current_user.username]
	else:
		jobs = [build_job_dict(jid, usernames) for jid in job_ids]
		other_jobs = []

	new_job_id = request.args.get("new", "")
	return render_template("active_jobs.html",
	                       jobs=jobs,
	                       other_jobs=other_jobs,
	                       is_admin=is_admin,
	                       new_job_id=new_job_id,
	                       active_section="active_jobs")


@bp.route("/results")
@login_required
def results():
	is_admin = current_user.role == "admin"
	# System Setting, read once per page (not per row)
	snapshot_days = current_app.backend.settings.get(
		"config_snapshot_retention_days")
	with current_app.backend.postgres.get_session() as db_session:
		if is_admin:
			raw_results = db_session.query(DeviceResult).all()
			metadata_rows = db_session.query(JobMetadata).all()
			usernames = {u.id: u.username for u in db_session.query(User).all()}
			endpoint_labels = {(row.ip, row.port): (row.label or row.ip)
			                   for row in db_session.query(Inventory).all()}
		else:
			user = db_session.get(User, current_user.id)
			raw_results = user.results
			metadata_rows = user.job_metadata
			usernames = {}
			endpoint_labels = visible_endpoint_labels(db_session,
			                                          current_user.id)
		db_session.expunge_all()

	metadata_by_job = {m.job_id: m for m in metadata_rows}

	if is_admin:
		my_raw = [r for r in raw_results if r.user_id == current_user.id]
		other_raw = [r for r in raw_results if r.user_id != current_user.id]
		jobs = build_jobs(my_raw, metadata_by_job, endpoint_labels,
		                  snapshot_days)
		jobs.sort(key=lambda x: x["completed_at"], reverse=True)
		other_jobs = []
		# group other_raw by user_id so each job gets its job_owner username
		other_raw_sorted = sorted(other_raw, key=lambda x: x.user_id)
		for user_id, user_rows in groupby(other_raw_sorted,
		                                  key=lambda x: x.user_id):
			owner = usernames.get(user_id, "unknown")
			other_jobs.extend(
				build_jobs(list(user_rows), metadata_by_job, endpoint_labels,
				           snapshot_days, job_owner=owner))
		other_jobs.sort(key=lambda x: x["completed_at"], reverse=True)
	else:
		jobs = build_jobs(raw_results, metadata_by_job, endpoint_labels,
		                  snapshot_days)
		jobs.sort(key=lambda x: x["completed_at"], reverse=True)
		other_jobs = []

	return render_template("results.html",
	                       active_section="results_30d",
	                       jobs=jobs,
	                       other_jobs=other_jobs,
	                       is_admin=is_admin,
	                       config_retention_days=snapshot_days)


@bp.route("/results/config_diff/<uuid:job_id>/<device_ip>")
@login_required
def config_diff(job_id, device_ip):
	# ?port= disambiguates devices sharing an IP within one job
	filters = {"job_id": job_id, "device_ip": device_ip}
	port = request.args.get("port", type=int)
	if port is not None:
		filters["device_port"] = port
	with current_app.backend.postgres.get_session() as db_session:
		row = db_session.query(DeviceResult).filter_by(**filters).first()
		if not row:
			return err("Not found", 404)
		if current_user.role != "admin" and row.user_id != current_user.id:
			return err("Forbidden", 403)
		config = row.fetched_config
		if config is None:
			return err(f"Config snapshot no longer available — snapshots are "
			           f"kept {current_app.backend.settings.get('config_snapshot_retention_days')} "
			           f"days", 410)
		meta = db_session.query(JobMetadata).filter_by(job_id=job_id).first()
		commands = meta.commands if meta else []
		device_type = row.device_type
	# The engine's own matcher, so the page can't disagree with the rollout
	# (commands with an unresolved $$TOKEN$$ come back "variable": the
	# rollout log has their verdicts with the device's values)
	verdicts = verify_commands(device_type, config, commands) \
		if device_type in PLATFORMS else []
	return ok(config=config, commands=commands,
	          verdicts=[[c, v] for c, v in zip(commands, verdicts)])


@bp.route("/results/summary/<uuid:job_id>")
@login_required
def job_summary(job_id):
	"""A finished job in a few lines, for the completion card on Active Jobs.
	404 until its results are stored (the log stream can end a moment
	before) — the card retries."""
	with current_app.backend.postgres.get_session() as db_session:
		rows = db_session.query(DeviceResult).filter_by(job_id=job_id).all()
		if not rows or (current_user.role != "admin"
		                and rows[0].user_id != current_user.id):
			return err("Not found", 404)
		labels = visible_endpoint_labels(db_session, current_user.id)
		meta = db_session.query(JobMetadata).filter_by(job_id=job_id).first()
		comment = meta.comment if meta else None
		db_session.expunge_all()

	def label(r):
		return labels.get((r.device_ip, r.device_port),
		                  r.device_ip if r.device_port == 22
		                  else f"{r.device_ip}:{r.device_port}")

	counts = {}
	for r in rows:
		counts[r.status] = counts.get(r.status, 0) + 1
	return ok(job_id=str(job_id), comment=comment,
	          status=job_status(rows), device_count=len(rows), counts=counts,
	          action_needed=[{"device": label(r), "text": r.action_needed}
	                         for r in rows if r.action_needed],
	          results_url=url_for("jobs.results", job=str(job_id)))


@bp.route("/results/download_log/<uuid:job_id>")
@login_required
def download_log(job_id):
	if current_user.role != "admin":
		owned = user_owns_job(job_id, current_user.id)
		if not owned:
			return Response("Not found", status=404)
	matches = glob.glob(os.path.join(runtime.logs_dir(), f"rollout_*_{job_id}.log"))
	if not matches:
		return Response("Log file not found", status=404)
	return send_file(matches[0], as_attachment=True,
	                 download_name=os.path.basename(matches[0]))
