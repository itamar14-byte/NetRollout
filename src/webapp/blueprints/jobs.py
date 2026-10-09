"""The dashboard, Active Jobs and Results: what ran, what runs now, each
job's devices and outcome, a device's config against the commands (Verify
Diff), a finished job's summary, and the rollout log's download."""
import glob
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby
from typing import Any

from flask import Blueprint, render_template, request, send_file, Response, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy import ColumnElement, and_, distinct, func, or_, tuple_
from sqlalchemy.orm import Session

from src import runtime
from src.db.tables import DeviceResult, JobMetadata, User, Inventory, Role
from src.inventory import visible_devices_clause
from src.jobs import (JobStore, RolloutJob, JOB_STATUSES, job_status,
                      job_status_condition, build_kpi)
from src.rollout.engine import endpoint
from src.rollout.platforms import PLATFORMS, verify_commands
from src.webapp.app import current_app
from src.webapp.http import ok, err


bp = Blueprint('jobs', __name__)

# Results' "entries per page" (an entry is a job, with all its devices)
PER_PAGE_CHOICES = (25, 50, 100, 250, 500)
DEFAULT_PER_PAGE = 100


@dataclass
class JobPage:
	"""One page of Results' jobs: their device results, and where the page
	sits among all the jobs in its scope."""
	results: list[DeviceResult]
	total: int  # jobs in the whole scope, not on this page
	page: int
	pages: int


def per_page_arg(raw: str | None) -> int:
	""":returns: the entries per page asked for when it is one of
	 PER_PAGE_CHOICES, else DEFAULT_PER_PAGE"""
	try:
		value = int(raw or "")
	except ValueError:
		return DEFAULT_PER_PAGE
	return value if value in PER_PAGE_CHOICES else DEFAULT_PER_PAGE


def status_arg(raw: str | None) -> str | None:
	""":returns: the job status asked for when it is one of JOB_STATUSES,
	 else None (no filter)"""
	return raw if raw in JOB_STATUSES else None


def page_arg(raw: str | None) -> int:
	""":returns: the 1-based page asked for; 1 when missing or not a positive
	 number"""
	try:
		return max(1, int(raw or ""))
	except ValueError:
		return 1


def load_job_page(db_session: Session, scope: ColumnElement[bool], page: int,
                  per_page: int, focus: uuid.UUID | None = None,
                  status: str | None = None) -> JobPage:
	"""One page of the jobs whose device results match scope, newest first
	(by their last device's completion; the job id breaks ties, so pages
	stay stable). The database pages the jobs (LIMIT/OFFSET), then only
	those jobs' device results are loaded.

	:param scope: which device results count, e.g. one user's
	:param page: the page asked for, 1-based; past the last one → the last
	:param per_page: jobs per page
	:param focus: a job to land on: when it is in scope, its page replaces
	 page
	:param status: only the jobs job_status gives this status (one of
	 JOB_STATUSES; judged on all their device results, all of which are
	 loaded); None: every job
	:returns: the page's device results, the jobs' total, the page shown and
	 the number of pages"""
	if status is not None:
		scope = and_(scope, DeviceResult.job_id.in_(
			db_session.query(DeviceResult.job_id).filter(scope)
			.group_by(DeviceResult.job_id)
			.having(job_status_condition(status))))
	done = func.max(DeviceResult.completed_at).label("done")
	total = db_session.query(func.count(distinct(DeviceResult.job_id)))\
		.filter(scope).scalar() or 0
	pages = max(1, -(-total // per_page))
	if focus is not None:
		focus_done = db_session.query(func.max(DeviceResult.completed_at))\
			.filter(scope, DeviceResult.job_id == focus).scalar()
		if focus_done is not None:
			jobs = db_session.query(DeviceResult.job_id, done).filter(scope)\
				.group_by(DeviceResult.job_id).subquery()
			before = db_session.query(func.count()).select_from(jobs).filter(
				or_(jobs.c.done > focus_done,
				    and_(jobs.c.done == focus_done, jobs.c.job_id > focus)))\
				.scalar() or 0
			page = before // per_page + 1
	page = min(max(page, 1), pages)
	job_ids = [row.job_id for row in
	           db_session.query(DeviceResult.job_id, done).filter(scope)
	           .group_by(DeviceResult.job_id)
	           .order_by(done.desc(), DeviceResult.job_id.desc())
	           .limit(per_page).offset((page - 1) * per_page)]
	results = db_session.query(DeviceResult).filter(
		scope, DeviceResult.job_id.in_(job_ids)).all() if job_ids else []
	return JobPage(results=results, total=total, page=page, pages=pages)


def visible_label_map(db_session: Session, user_id: uuid.UUID) -> dict[str, str]:
	"""ip → label over the user's own and global devices; their own win
	when one shares an IP with a global one."""
	rows = db_session.query(Inventory.ip, Inventory.label, Inventory.user_id)\
		.filter(visible_devices_clause(user_id)).all()
	rows.sort(key=lambda r: r.user_id == user_id)
	return {r.ip: r.label for r in rows}


def visible_endpoint_labels(db_session: Session,
                            user_id: uuid.UUID) -> dict[tuple[str, int], str]:
	"""(ip, port) → label, to name each device: the IP alone is ambiguous
	when several share it (NAT, port forwarding). Their own win."""
	rows = db_session.query(Inventory.ip, Inventory.port, Inventory.label,
	                        Inventory.user_id)\
		.filter(visible_devices_clause(user_id)).all()
	rows.sort(key=lambda r: r.user_id == user_id)
	return {(r.ip, r.port): r.label for r in rows}


def shown_endpoint_labels(db_session: Session, results: Iterable[DeviceResult]) 		-> dict[tuple[str, int], str]:
	"""(ip, port) → label (else the IP) for these device results' devices, from
	anyone's inventory - what an admin sees (Results, the completion card);
	only those devices are read.

	:param results: the device results being shown"""
	shown = {(r.device_ip, r.device_port) for r in results}
	if not shown:
		return {}
	return {(row.ip, row.port): (row.label or row.ip)
	        for row in db_session.query(Inventory.ip, Inventory.port, Inventory.label)
	        .filter(tuple_(Inventory.ip, Inventory.port).in_(shown))}


def user_owns_job(job_id: uuid.UUID, user_id: uuid.UUID) -> bool:
	""":returns: whether the job's results are the user's"""
	with current_app.backend.postgres.get_session() as db_session:
		return bool(db_session.query(DeviceResult).filter_by(
			job_id=job_id, user_id=user_id).first())


def load_dashboard_data(user_id: uuid.UUID, kpi_user_id: uuid.UUID,
                        is_admin: bool) -> dict[str, Any]:
	"""The dashboard's data: the user's own counts and results, and the KPIs'
	results - of another user when an admin picked one.

	:param kpi_user_id: whose last 30 days the KPIs show
	:param is_admin: the user list (for that choice) is loaded"""
	with current_app.backend.postgres.get_session() as db_session:
		# Current user's dashboard content (always own data)
		user = db_session.get(User, user_id)
		if user is None:
			raise LookupError(f"no user {user_id}")
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


def build_job_summaries(results: Iterable[DeviceResult]) -> list[dict[str, Any]]:
	""":returns: one summary per job (completed_at, device_count,
	 commands_sent, status, action_needed), newest first"""
	sorted_results = sorted(results, key=lambda x: x.job_id)
	summaries: list[dict[str, Any]] = []
	for job_id, group in groupby(sorted_results, key=lambda x: x.job_id):
		rows = list(group)
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


def get_active_job(user_id: uuid.UUID) -> RolloutJob | None:
	""":returns: one of the user's rollouts of this process not over yet - a
	 running one first, else a queued one; None: none"""
	job_ids = JobStore(current_app.backend.redis).job_ids(user_id)
	jobs = [j for jid in job_ids
	        if (j := current_app.orchestrator.get_job(uuid.UUID(jid))) and not j.is_over()]
	return next((j for j in jobs if j.started_at is not None), next(iter(jobs), None))


def config_expired(row: DeviceResult, snapshot_days: int) -> bool:
	"""Whether a device's config snapshot was there and is gone: one is kept
	only when verify found a mismatch, and the nightly clean-up clears it
	after the snapshot retention."""
	verify_mismatch = (row.commands_verified is not None
	                   and row.commands_verified < row.commands_sent)
	too_old = row.completed_at < datetime.now() - timedelta(
		days=snapshot_days)
	return verify_mismatch and row.fetched_config is None and too_old


def build_jobs(result_rows: Iterable[DeviceResult],
               metadata_by_job: dict[uuid.UUID, JobMetadata],
               endpoint_labels: dict[tuple[str, int], str], snapshot_days: int,
               job_owner: str | None = None) -> list[dict[str, Any]]:
	"""The Results page's jobs: each with its times, status, comment,
	commands and devices (no configs - fetched on demand by config_diff).

	:param endpoint_labels: (ip, port) → the label to show
	:param snapshot_days: the config snapshot retention (to say a snapshot
	 expired)
	:param job_owner: the owner's username, shown on other users' jobs"""
	sorted_rows = sorted(result_rows, key=lambda x: x.job_id)
	out: list[dict[str, Any]] = []
	for job_id, group in groupby(sorted_rows, key=lambda x: x.job_id):
		rows = list(group)
		meta = metadata_by_job.get(job_id)
		log_matches = glob.glob(
			os.path.join(runtime.logs_dir(), f"rollout_*_{job_id}.log"))
		entry: dict[str, Any] = {
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
						else endpoint(r.device_ip, r.device_port)),
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


def build_job_dict(job_id: str, usernames: dict[str, str]) -> dict[str, Any]:
	"""An Active Jobs row from the job's Redis state (and the job itself when
	it runs in this process).

	:param usernames: user id → username, to name the owner"""
	meta = JobStore(current_app.backend.redis).meta(job_id)
	job = current_app.orchestrator.get_job(uuid.UUID(job_id))
	# Not in memory (another process's, or ended meanwhile): the stored
	# count - the page sums device counts, always an int
	stored_count = meta.device_count if meta else 0
	return {
		"id": job_id,
		"status": meta.status if meta and meta.status else "unknown",
		"created_at": meta.created_at if meta else "",
		"device_count": job.get_device_count() if job else stored_count,
		"started_at": job.started_at.strftime(
			"%H:%M:%S") if job and job.started_at else "—",
		"started_at_iso": job.started_at.isoformat() if job and job.started_at else "",
		"owner": usernames.get(meta.user_id, "unknown") if meta else "unknown"
	}


@bp.route("/dashboard")
@login_required
def dashboard() -> str:
	"""The dashboard: counts, recent jobs, the user's running rollout, and
	the 30-day KPIs - an admin's of any user (?user=<id>)."""
	# ── Admin KPI scope ───────────────────────────────────────────────────────
	is_admin = current_user.role == Role.ADMIN
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
		started = active_job.started_at
		active_job_data = {
			"job_id": str(active_job.job_id),
			"device_count": active_job.get_device_count(),
			"queued": started is None,
			"started_at": started.strftime("%H:%M:%S") if started else "—",
			"started_at_iso": started.isoformat() if started else "",
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
def active_jobs() -> str:
	"""Active Jobs: the user's queued and running rollouts - and, for an
	admin, everyone else's apart."""
	is_admin = current_user.role == Role.ADMIN
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
def results() -> str:
	"""Results: the user's jobs - and, for an admin, everyone else's apart,
	with their owners - newest first, a page at a time: ?per_page= (one of
	PER_PAGE_CHOICES, else 100), ?page= (the user's own jobs), ?other_page=
	(an admin's other users' jobs), ?view=all (an admin's all-users view),
	?status= (one of JOB_STATUSES: only those jobs, in both of an admin's
	lists; else every job); ?job=<id> without ?page= lands on that job's
	page - for an admin, another user's job without ?other_page= on its page
	of the all-users view."""
	is_admin = current_user.role == Role.ADMIN
	per_page = per_page_arg(request.args.get("per_page"))
	status = status_arg(request.args.get("status"))
	focus = None
	if "page" not in request.args:
		try:
			focus = uuid.UUID(request.args.get("job", ""))
		except ValueError:
			pass
	# System Setting, read once per page (not per row)
	snapshot_days = current_app.backend.settings.get(
		"config_snapshot_retention_days")
	with current_app.backend.postgres.get_session() as db_session:
		mine = load_job_page(db_session, DeviceResult.user_id == current_user.id,
		                     page_arg(request.args.get("page")), per_page,
		                     focus, status)
		if is_admin:
			# ?job= of another user's job (no ?other_page=): its page
			other_focus = focus if ("other_page" not in request.args and focus
			                        not in {r.job_id for r in mine.results}) \
				else None
			others = load_job_page(
				db_session, DeviceResult.user_id != current_user.id,
				page_arg(request.args.get("other_page")), per_page,
				other_focus, status)
			focus_in_others = other_focus is not None and other_focus in {
				r.job_id for r in others.results}
			owner_ids = {r.user_id for r in others.results}
			usernames = {u.id: u.username for u in db_session.query(User)
			             .filter(User.id.in_(owner_ids))} if owner_ids else {}
			endpoint_labels = shown_endpoint_labels(db_session,
			                                        mine.results + others.results)
		else:
			others = JobPage(results=[], total=0, page=1, pages=1)
			focus_in_others = False
			usernames = {}
			endpoint_labels = visible_endpoint_labels(db_session,
			                                          current_user.id)
		page_job_ids = {r.job_id for r in mine.results + others.results}
		metadata_rows = db_session.query(JobMetadata).filter(
			JobMetadata.job_id.in_(page_job_ids)).all() if page_job_ids else []
		db_session.expunge_all()

	metadata_by_job = {m.job_id: m for m in metadata_rows}

	if is_admin:
		jobs = build_jobs(mine.results, metadata_by_job, endpoint_labels,
		                  snapshot_days)
		jobs.sort(key=lambda x: x["completed_at"], reverse=True)
		other_jobs: list[dict[str, Any]] = []
		# group the others' results by user_id so each job gets its
		# job_owner username
		other_raw_sorted = sorted(others.results, key=lambda x: x.user_id)
		for user_id, user_rows in groupby(other_raw_sorted,
		                                  key=lambda x: x.user_id):
			owner = usernames.get(user_id, "unknown")
			other_jobs.extend(
				build_jobs(list(user_rows), metadata_by_job, endpoint_labels,
				           snapshot_days, job_owner=owner))
		other_jobs.sort(key=lambda x: x["completed_at"], reverse=True)
	else:
		jobs = build_jobs(mine.results, metadata_by_job, endpoint_labels,
		                  snapshot_days)
		jobs.sort(key=lambda x: x["completed_at"], reverse=True)
		other_jobs = []

	return render_template("results.html",
	                       active_section="results_30d",
	                       jobs=jobs,
	                       other_jobs=other_jobs,
	                       mine=mine,
	                       others=others,
	                       per_page=per_page,
	                       per_page_choices=PER_PAGE_CHOICES,
	                       status_filter=status,
	                       job_statuses=JOB_STATUSES,
	                       split_view=is_admin
	                       and (request.args.get("view") == "all"
	                            or focus_in_others),
	                       is_admin=is_admin,
	                       config_retention_days=snapshot_days)


@bp.route("/results/config_diff/<uuid:job_id>/<device_ip>")
@login_required
def config_diff(job_id: uuid.UUID, device_ip: str) -> ResponseReturnValue:
	"""Verify Diff: a device's fetched config with each command's verdict,
	from the engine's own matcher. ?port= picks one of the devices sharing
	the IP in the job.

	:returns: {config, commands, verdicts: [[command, verdict]]}; 404 (also
	 for another user's job: it isn't revealed), or 410 once the snapshot is gone"""
	filters: dict[str, Any] = {"job_id": job_id, "device_ip": device_ip}
	port = request.args.get("port", type=int)
	if port is not None:
		filters["device_port"] = port
	with current_app.backend.postgres.get_session() as db_session:
		row = db_session.query(DeviceResult).filter_by(**filters).first()
		if not row or (current_user.role != Role.ADMIN and row.user_id != current_user.id):
			return err("Not found", 404)
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
def job_summary(job_id: uuid.UUID) -> ResponseReturnValue:
	"""A finished job in a few lines, for the completion card on Active Jobs.
	404 until its results are stored (the log stream can end a moment
	before) — the card retries."""
	with current_app.backend.postgres.get_session() as db_session:
		rows = db_session.query(DeviceResult).filter_by(job_id=job_id).all()
		if not rows or (current_user.role != Role.ADMIN
		                and rows[0].user_id != current_user.id):
			return err("Not found", 404)
		# an admin names devices as on Results (anyone's inventory)
		labels = shown_endpoint_labels(db_session, rows) 			if current_user.role == Role.ADMIN 			else visible_endpoint_labels(db_session, current_user.id)
		meta = db_session.query(JobMetadata).filter_by(job_id=job_id).first()
		comment = meta.comment if meta else None
		db_session.expunge_all()

	def label(r: DeviceResult) -> str:
		return labels.get((r.device_ip, r.device_port),
		                  r.device_ip if r.device_port == 22
		                  else endpoint(r.device_ip, r.device_port))

	counts: dict[str, int] = {}
	for r in rows:
		counts[r.status] = counts.get(r.status, 0) + 1
	return ok(job_id=str(job_id), comment=comment,
	          status=job_status(rows), device_count=len(rows), counts=counts,
	          action_needed=[{"device": label(r), "text": r.action_needed}
	                         for r in rows if r.action_needed],
	          results_url=url_for("jobs.results", job=str(job_id)))


@bp.route("/results/download_log/<uuid:job_id>")
@login_required
def download_log(job_id: uuid.UUID) -> ResponseReturnValue:
	"""The job's rollout log file - its owner's, or any for an admin."""
	if current_user.role != Role.ADMIN:
		owned = user_owns_job(job_id, current_user.id)
		if not owned:
			return Response("Not found", status=404)
	matches = glob.glob(os.path.join(runtime.logs_dir(), f"rollout_*_{job_id}.log"))
	if not matches:
		return Response("Log file not found", status=404)
	return send_file(matches[0], as_attachment=True,
	                 download_name=os.path.basename(matches[0]))
