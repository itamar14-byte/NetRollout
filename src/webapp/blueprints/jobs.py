# python utilities
import glob
import os
import uuid
from datetime import datetime, timedelta
from itertools import groupby

# services
# flask
from flask import (Blueprint, render_template, current_app, request, send_file,
                   Response)
from flask_login import current_user, login_required

# local modules
from src.db.tables import DeviceResult, JobMetadata, User, Inventory
from src.logging_utils import LOGS_DIR
from src.webapp.utils import ok, err

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
		inv_label_map = {d.ip: d.label for d in user.inventory}

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
			kpi_label_map = {
				row.ip: row.label
				for row in db_session.query(Inventory.ip, Inventory.label)
				.filter(Inventory.user_id == kpi_user_id).all()
			}

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
		})
	summaries.sort(key=lambda x: x["completed_at"], reverse=True)
	return summaries


def get_active_job(user_id):
	raw_ids = current_app.backend.redis.client.smembers(f"user_jobs:{user_id}")
	job_ids = [jid.decode() for jid in raw_ids]
	return next(
		(j for jid in job_ids
		 if (j := current_app.orchestrator.get_job(
			uuid.UUID(jid))) and j.is_alive()),
		None
	)


def build_jobs(result_rows, metadata_by_job, ip_to_label, job_owner=None):
	sorted_rows = sorted(result_rows, key=lambda x: x.job_id)
	out = []
	for job_id, rows in groupby(sorted_rows, key=lambda x: x.job_id):
		rows = list(rows)
		meta = metadata_by_job.get(job_id)
		log_matches = glob.glob(
			os.path.join(LOGS_DIR, f"rollout_*_{job_id}.log"))
		entry = {
			"job_id": str(job_id),
			"has_log": bool(log_matches),
			"started_at": min(r.started_at for r in rows),
			"completed_at": max(r.completed_at for r in rows),
			"device_count": len(rows),
			"commands_sent": rows[0].commands_sent,
			"status": job_status(rows),
			"comment": meta.comment if meta else None,
			"commands": meta.commands if meta else [],
			"devices": [
				{
					"ip": r.device_ip,
					"label": ip_to_label.get(r.device_ip, r.device_ip),
					"device_type": r.device_type,
					"status": r.status,
					"commands_sent": r.commands_sent,
					"commands_verified": r.commands_verified,
					"fetched_config": r.fetched_config
				}
				for r in rows
			]
		}
		if job_owner is not None:
			entry["job_owner"] = job_owner
		out.append(entry)
	return out


def build_job_dict(job_id, usernames):
	meta = {k.decode(): v.decode() for k, v in
	        current_app.backend.redis.client.hgetall(
		        f"job:{job_id}:meta").items()}
	job = current_app.orchestrator.get_job(uuid.UUID(job_id))
	return {
		"id": job_id,
		"status": meta.get("status", "unknown"),
		"created_at": meta.get("created_at", ""),
		"device_count": job.get_device_count() if job else meta.get(
			"device_count", "—"),
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
	kpi = current_app.web.build_kpi(data["kpi_results_30d"],
	                                data["kpi_label_map"])

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
	with current_app.backend.postgres.get_session() as db_session:
		if is_admin:
			job_ids = [k.decode().split(":")[1] for k in
			           current_app.backend.redis.client.scan_iter("job:*:meta")]
			usernames = {str(u.id): u.username for u in
			             db_session.query(User).all()}
		else:
			job_ids = [jid.decode() for jid in
			           current_app.backend.redis.client.smembers(
				           f"user_jobs:{current_user.id}")]
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
	with current_app.backend.postgres.get_session() as db_session:
		if is_admin:
			raw_results = db_session.query(DeviceResult).all()
			metadata_rows = db_session.query(JobMetadata).all()
			usernames = {u.id: u.username for u in db_session.query(User).all()}
			inv_rows = db_session.query(Inventory).all()
		else:
			user = db_session.get(User, current_user.id)
			raw_results = user.results
			metadata_rows = user.job_metadata
			usernames = {}
			inv_rows = user.inventory
		ip_to_label = {row.ip: (row.label or row.ip) for row in inv_rows}
		db_session.expunge_all()

	metadata_by_job = {m.job_id: m for m in metadata_rows}

	if is_admin:
		my_raw = [r for r in raw_results if r.user_id == current_user.id]
		other_raw = [r for r in raw_results if r.user_id != current_user.id]
		jobs = build_jobs(my_raw, metadata_by_job, ip_to_label)
		jobs.sort(key=lambda x: x["completed_at"], reverse=True)
		other_jobs = []
		# group other_raw by user_id so each job gets its job_owner username
		other_raw_sorted = sorted(other_raw, key=lambda x: x.user_id)
		for user_id, user_rows in groupby(other_raw_sorted,
		                                  key=lambda x: x.user_id):
			owner = usernames.get(user_id, "unknown")
			other_jobs.extend(
				build_jobs(list(user_rows), metadata_by_job, ip_to_label,
				            job_owner=owner))
		other_jobs.sort(key=lambda x: x["completed_at"], reverse=True)
	else:
		jobs = build_jobs(raw_results, metadata_by_job, ip_to_label)
		jobs.sort(key=lambda x: x["completed_at"], reverse=True)
		other_jobs = []

	return render_template("results.html",
	                       active_section="results_30d",
	                       jobs=jobs,
	                       other_jobs=other_jobs,
	                       is_admin=is_admin)


@bp.route("/results/config_diff/<uuid:job_id>/<device_ip>")
@login_required
def config_diff(job_id, device_ip):
	with current_app.backend.postgres.get_session() as db_session:
		row = db_session.query(DeviceResult).filter_by(
			job_id=job_id, device_ip=device_ip).first()
		if not row:
			return err("Not found", 404)
		if current_user.role != "admin" and row.user_id != current_user.id:
			return err("Forbidden", 403)
		config = row.fetched_config
		meta = db_session.query(JobMetadata).filter_by(job_id=job_id).first()
		commands = meta.commands if meta else []
	return ok(config=config, commands=commands)


@bp.route("/results/download_log/<uuid:job_id>")
@login_required
def download_log(job_id):
	if current_user.role != "admin":
		owned = user_owns_job(job_id, current_user.id)
		if not owned:
			return Response("Not found", status=404)
	matches = glob.glob(os.path.join(LOGS_DIR, f"rollout_*_{job_id}.log"))
	if not matches:
		return Response("Log file not found", status=404)
	return send_file(matches[0], as_attachment=True,
	                 download_name=os.path.basename(matches[0]))
