"""The dashboard, Active Jobs and Results: what ran, what runs now, each
job's devices and outcome, a device's config against the commands (Verify
Diff), a finished job's summary, and the rollout log's download. The
finished jobs' history and who may see it: src/results.py."""
import os
import uuid
from itertools import groupby
from typing import Any

from flask import Blueprint, render_template, request, send_file, Response, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required

from src.accounts.users import Accounts, Viewer
from src.db.tables import User
from src.inventory import InventoryView, LabelScope
from src.jobs import JobStore, RolloutJob
from src.results import (JOB_STATUSES, JobPage, JobResults, JobScope, build_job_summaries,
                         build_jobs, build_kpi, log_file)
from src.rollout.platforms import PLATFORMS, verify_commands
from src.webapp.app import current_app
from src.webapp.http import ok, err, viewer


bp = Blueprint('jobs', __name__)

# Results' "entries per page" (an entry is a job, with all its devices)
PER_PAGE_CHOICES = (25, 50, 100, 250, 500)
DEFAULT_PER_PAGE = 100


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


def load_dashboard_data(picked: str | None) -> dict[str, Any]:
	"""The dashboard's data: the signed-in user's own counts and results, and
	the KPIs' results - of another user when an admin picked one.

	:param picked: whose last 30 days the KPIs show (?user=, an admin's
	 choice - JobResults.scope_user)"""
	me = viewer()
	with current_app.backend.postgres.get_session() as db_session:
		kpi_user_id, selected_user = JobResults(db_session, me).scope_user(picked)
		# Current user's dashboard content (always own data)
		user = db_session.get(User, me.id)
		if user is None:
			raise LookupError(f"no user {me.id}")
		inventory_count = len(user.inventory)
		profile_count = len(user.security_profiles)
		mapping_count = len(user.variable_mappings)
		jobs_results = user.results

		# KPI data — scoped to kpi_user_id (may differ from current user for admin)
		kpi_results_30d = JobResults(db_session, me).recent(kpi_user_id)
		kpi_label_map = InventoryView(db_session, Viewer(kpi_user_id)).label_map(
			LabelScope.VISIBLE)

		users = Accounts(db_session).for_admin_picker() if me.is_admin else []
		db_session.expunge_all()

	return {
		"inventory_count": inventory_count,
		"profile_count": profile_count,
		"mapping_count": mapping_count,
		"jobs_results": jobs_results,
		"kpi_results_30d": kpi_results_30d,
		"kpi_label_map": kpi_label_map,
		"users": users,
		"selected_user": selected_user,
	}


def get_active_job(user_id: uuid.UUID) -> RolloutJob | None:
	""":returns: one of the user's rollouts of this process not over yet - a
	 running one first, else a queued one; None: none"""
	job_ids = JobStore(current_app.backend.redis).job_ids(user_id)
	jobs = [j for jid in job_ids
	        if (j := current_app.orchestrator.get_job(uuid.UUID(jid))) and not j.is_over()]
	return next((j for j in jobs if j.started_at is not None), next(iter(jobs), None))


def build_job_dict(job_id: str, usernames: dict[str, str]) -> dict[str, Any]:
	"""An Active Jobs row from the job's Redis state (and the job itself when
	it runs in this process).

	:param usernames: user id → username, to name the owner"""
	meta = JobStore(current_app.backend.redis).meta(job_id)
	job = current_app.orchestrator.get_job(uuid.UUID(job_id))
	# Not in memory (another process's, or ended meanwhile): its stored row -
	# the page sums device counts, always an int
	row = job.row() if job else JobStore.row(job_id, meta) if meta else None
	return {
		"id": job_id,
		"status": meta.status if meta and meta.status else "unknown",
		"created_at": meta.created_at if meta else "",
		"device_count": row.devices if row else 0,
		"started_at": row.clock if row else "—",
		"started_at_iso": row.started or "" if row else "",
		"owner": usernames.get(meta.user_id, "unknown") if meta else "unknown"
	}


@bp.route("/dashboard")
@login_required
def dashboard() -> str:
	"""The dashboard: counts, recent jobs, the user's running rollout, and
	the 30-day KPIs - an admin's of any user (?user=<id>)."""
	data = load_dashboard_data(request.args.get("user"))   # an admin's KPI scope
	selected_user = data["selected_user"]
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
		row = active_job.row()
		active_job_data = {
			"job_id": row.job_id,
			"device_count": row.devices,
			"queued": row.started is None,
			"started_at": row.clock,
			"started_at_iso": row.started or "",
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
	is_admin = viewer().is_admin
	store = JobStore(current_app.backend.redis)
	with current_app.backend.postgres.get_session() as db_session:
		if is_admin:
			job_ids = store.job_ids()
			usernames = {str(u.id): u.username for u in
			             Accounts(db_session).for_admin_picker()}
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
	is_admin = viewer().is_admin
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
		history = JobResults(db_session, viewer())
		mine = history.page(JobScope.MINE, page_arg(request.args.get("page")), per_page,
		                    focus, status)
		if is_admin:
			# ?job= of another user's job (no ?other_page=): its page
			other_focus = focus if ("other_page" not in request.args and focus
			                        not in {r.job_id for r in mine.results}) \
				else None
			others = history.page(JobScope.OTHERS, page_arg(request.args.get("other_page")),
			                      per_page, other_focus, status)
			focus_in_others = other_focus is not None and other_focus in {
				r.job_id for r in others.results}
			usernames = history.owner_names(others.results)
		else:
			others = JobPage(results=[], total=0, page=1, pages=1)
			focus_in_others = False
			usernames = {}
		endpoint_labels = history.device_labels(mine.results + others.results)
		metadata_by_job = history.metadata(r.job_id for r in mine.results + others.results)
		db_session.expunge_all()

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
	with current_app.backend.postgres.get_session() as db_session:
		snapshot = JobResults(db_session, viewer()).config_snapshot(
			job_id, device_ip, request.args.get("port", type=int))
	if snapshot is None:
		return err("Not found", 404)
	if snapshot.config is None:
		return err(f"Config snapshot no longer available — snapshots are "
		           f"kept {current_app.backend.settings.get('config_snapshot_retention_days')} "
		           f"days", 410)
	# The engine's own matcher, so the page can't disagree with the rollout
	# (commands with an unresolved $$TOKEN$$ come back "variable": the
	# rollout log has their verdicts with the device's values)
	verdicts = verify_commands(snapshot.device_type, snapshot.config, snapshot.commands) \
		if snapshot.device_type in PLATFORMS else []
	return ok(config=snapshot.config, commands=snapshot.commands,
	          verdicts=[[c, v] for c, v in zip(snapshot.commands, verdicts)])


@bp.route("/results/summary/<uuid:job_id>")
@login_required
def job_summary(job_id: uuid.UUID) -> ResponseReturnValue:
	"""A finished job in a few lines, for the completion card on Active Jobs.
	404 until its results are stored (the log stream can end a moment
	before) — the card retries."""
	with current_app.backend.postgres.get_session() as db_session:
		summary = JobResults(db_session, viewer()).summary(job_id)
	if summary is None:
		return err("Not found", 404)
	return ok(**summary, results_url=url_for("jobs.results", job=str(job_id)))


@bp.route("/results/download_log/<uuid:job_id>")
@login_required
def download_log(job_id: uuid.UUID) -> ResponseReturnValue:
	"""The job's rollout log file - its owner's, or any for an admin (even of
	a job whose results weren't stored)."""
	if not viewer().is_admin:
		with current_app.backend.postgres.get_session() as db_session:
			owned = JobResults(db_session, viewer()).get_accessible(job_id) is not None
		if not owned:
			return Response("Not found", status=404)
	path = log_file(job_id)
	if path is None:
		return Response("Log file not found", status=404)
	return send_file(path, as_attachment=True, download_name=os.path.basename(path))
