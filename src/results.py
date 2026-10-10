"""Finished jobs' history (web app): the device results and job metadata a
rollout leaves in Postgres, as one user - the viewer - may see them: their
own jobs, any job for an admin (the one rule, JobResults.may_see; a job
someone may not see is answered as one that doesn't exist). Results' pages
of jobs, a job's summary, a device's config snapshot, the successful
devices a rollback targets, a user's totals for the Account page, the last
30 days for the dashboard and Analytics (build_kpi), a job's status from its
devices' (job_status, and in SQL job_status_condition), and the rollout log
files."""
from __future__ import annotations   # type hints are never evaluated

import glob
import os
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import groupby
from typing import Any

from sqlalchemy import ColumnElement, and_, distinct, func, not_, or_
from sqlalchemy.orm import Session

from src import runtime
from src.accounts.users import Viewer
from src.db.tables import DeviceResult, JobMetadata, User
from src.inventory import InventoryView, LabelScope, Target
from src.rollout.engine import DeviceStatus, endpoint


# what "the last 30 days" means on the dashboard and Analytics
RECENT_DAYS = 30
# the most rows the query builder answers with
QUERY_LIMIT = 200


def log_file(job_id: uuid.UUID | str) -> str | None:
	""":returns: the job's rollout log file; None when there's none (any
	 more)"""
	matches = glob.glob(os.path.join(runtime.logs_dir(), f"rollout_*_{job_id}.log"))
	return matches[0] if matches else None


def config_expired(row: DeviceResult, snapshot_days: int) -> bool:
	"""Whether a device's config snapshot was there and is gone: one is kept
	only when verify found a mismatch, and the nightly clean-up clears it
	after the snapshot retention."""
	verify_mismatch = (row.commands_verified is not None
	                   and row.commands_verified < row.commands_sent)
	too_old = row.completed_at < datetime.now() - timedelta(
		days=snapshot_days)
	return verify_mismatch and row.fetched_config is None and too_old


def job_status(rows: Sequence[DeviceResult]) -> DeviceStatus:
	""":returns: a job's status from its devices': cancelled if any was; failed
	 if all failed; partial if any failed or was partial; else success"""
	statuses = {r.status for r in rows}
	if DeviceStatus.CANCELLED in statuses:
		return DeviceStatus.CANCELLED
	if all(r.status == DeviceStatus.FAILED for r in rows):
		return DeviceStatus.FAILED
	if any(r.status in (DeviceStatus.FAILED, DeviceStatus.PARTIAL) for r in rows):
		return DeviceStatus.PARTIAL
	return DeviceStatus.SUCCESS


# what job_status can say, in the Results page's filter order
JOB_STATUSES = tuple(DeviceStatus)


def job_status_condition(status: str) -> ColumnElement[bool]:
	"""job_status' rules in SQL, over one job's device results (a HAVING
	condition of a query grouped by job_id): true exactly for the jobs
	job_status calls status.

	:param status: one of JOB_STATUSES
	:raises ValueError: any other status"""
	def any_device(*statuses: DeviceStatus) -> ColumnElement[bool]:
		return func.bool_or(DeviceResult.status.in_([s.value for s in statuses]))

	all_failed = func.bool_and(DeviceResult.status == DeviceStatus.FAILED.value)
	if status == DeviceStatus.CANCELLED:
		return any_device(DeviceStatus.CANCELLED)
	if status == DeviceStatus.FAILED:
		return and_(not_(any_device(DeviceStatus.CANCELLED)), all_failed)
	if status == DeviceStatus.PARTIAL:
		return and_(not_(any_device(DeviceStatus.CANCELLED)), not_(all_failed),
		            any_device(DeviceStatus.FAILED, DeviceStatus.PARTIAL))
	if status == DeviceStatus.SUCCESS:
		return not_(any_device(DeviceStatus.CANCELLED, DeviceStatus.FAILED,
		                       DeviceStatus.PARTIAL))
	raise ValueError(f"no job status {status!r}")


def device_label(labels: dict[Target, str], row: DeviceResult) -> str:
	""":returns: how a device result's device is named: its label, else its
	 IP - with the port unless it's plain SSH, so devices sharing an IP stay
	 distinguishable"""
	return labels.get((row.device_ip, row.device_port),
	                  row.device_ip if row.device_port == 22
	                  else endpoint(row.device_ip, row.device_port))


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


def build_jobs(result_rows: Iterable[DeviceResult],
               metadata_by_job: dict[uuid.UUID, JobMetadata],
               endpoint_labels: dict[Target, str], snapshot_days: int,
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
		entry: dict[str, Any] = {
			"job_id": str(job_id),
			"has_log": log_file(job_id) is not None,
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
					"label": device_label(endpoint_labels, r),
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


def build_kpi(results_30d: Sequence[DeviceResult],
              label_map: dict[str, str]) -> dict[str, Any]:
	"""The dashboard's tiles from the last 30 days' device results.

	:param label_map: device IP → its label, to name the most-failed device
	:returns: success_rate (%, None without results), jobs_30d,
	 device_pushes (device results: a device pushed to in a rollout, failed
	 ones too - not distinct devices), commands_pushed, top_failed ({ip,
	 label, fail_count} or None)"""
	total_ops = len(results_30d)
	jobs_30d = len({r.job_id for r in results_30d})
	success_count = sum(1 for r in results_30d if r.status == DeviceStatus.SUCCESS)

	fail_counts_ip: dict[str, int] = defaultdict(int)
	for r in results_30d:
		if r.status == DeviceStatus.FAILED:
			fail_counts_ip[r.device_ip] += 1
	top_failed = None
	if fail_counts_ip:
		top_ip = max(fail_counts_ip, key=lambda ip: fail_counts_ip[ip])
		top_failed = {"ip": top_ip, "label": label_map.get(top_ip),
		              "fail_count": fail_counts_ip[top_ip]}

	return {
		"success_rate": round(
			success_count / total_ops * 100) if total_ops else None,
		"jobs_30d": jobs_30d,
		"device_pushes": total_ops,
		"commands_pushed": sum(r.commands_sent for r in results_30d),
		"top_failed": top_failed
	}


@dataclass
class JobPage:
	"""One page of Results' jobs: their device results, and where the page
	sits among all the jobs in its scope."""
	results: list[DeviceResult]
	total: int  # jobs in the whole scope, not on this page
	page: int
	pages: int


class JobScope(StrEnum):
	"""Whose jobs a Results list shows."""
	MINE = "mine"       # the viewer's
	OTHERS = "others"   # everyone else's (an admin's second list)


@dataclass(frozen=True)
class ConfigSnapshot:
	"""A device's fetched config in a job, with the job's commands."""
	config: str | None   # None: the snapshot is gone (or was never kept)
	commands: list[str]
	device_type: str


class JobResults:
	"""Finished jobs as the viewer may see them: their own, and any for an
	admin. Reads only; works in the caller's session."""

	def __init__(self, session: Session, viewer: Viewer) -> None:
		self.session = session
		self.viewer = viewer

	# ── who may see a job ──

	def may_see(self, owner_id: uuid.UUID) -> bool:
		"""The one rule: a job's owner sees it, and so does any admin."""
		return self.viewer.is_admin or owner_id == self.viewer.id

	def get_accessible(self, job_id: uuid.UUID) -> uuid.UUID | None:
		""":returns: the job's owner; None when the job has no results (yet) -
		 or the viewer may not see it (the same answer: its existence isn't
		 revealed)"""
		first = self.session.query(DeviceResult.user_id).filter_by(job_id=job_id).first()
		if first is None or not self.may_see(first.user_id):
			return None
		return first.user_id

	def scope_user(self, raw: str | None) -> tuple[uuid.UUID, str]:
		"""Whose numbers a page shows: an admin may pick any user (?user=<id>,
		"me" or anything else: their own); anyone else sees their own.

		:returns: (the user's id, "me" or the id as given)"""
		param = (raw or "me").strip()
		if self.viewer.is_admin and param != "me":
			try:
				return uuid.UUID(param), param
			except ValueError:
				pass
		return self.viewer.id, "me"

	# ── Results ──

	def page(self, scope: JobScope, page: int, per_page: int,
	         focus: uuid.UUID | None = None, status: str | None = None) -> JobPage:
		"""One page of the scope's jobs, newest first (by their last device's
		completion; the job id breaks ties, so pages stay stable). The
		database pages the jobs (LIMIT/OFFSET), then only those jobs' device
		results are loaded.

		:param page: the page asked for, 1-based; past the last one → the last
		:param per_page: jobs per page
		:param focus: a job to land on: when it is in scope, its page replaces
		 page
		:param status: only the jobs job_status gives this status (one of
		 JOB_STATUSES; judged on all their device results, all of which are
		 loaded); None: every job
		:returns: the page's device results, the jobs' total, the page shown
		 and the number of pages"""
		where: ColumnElement[bool] = (DeviceResult.user_id == self.viewer.id
		                              if scope is JobScope.MINE
		                              else DeviceResult.user_id != self.viewer.id)
		db = self.session
		if status is not None:
			where = and_(where, DeviceResult.job_id.in_(
				db.query(DeviceResult.job_id).filter(where)
				.group_by(DeviceResult.job_id)
				.having(job_status_condition(status))))
		done = func.max(DeviceResult.completed_at).label("done")
		total = db.query(func.count(distinct(DeviceResult.job_id)))\
			.filter(where).scalar() or 0
		pages = max(1, -(-total // per_page))
		if focus is not None:
			focus_done = db.query(func.max(DeviceResult.completed_at))\
				.filter(where, DeviceResult.job_id == focus).scalar()
			if focus_done is not None:
				jobs = db.query(DeviceResult.job_id, done).filter(where)\
					.group_by(DeviceResult.job_id).subquery()
				before = db.query(func.count()).select_from(jobs).filter(
					or_(jobs.c.done > focus_done,
					    and_(jobs.c.done == focus_done, jobs.c.job_id > focus)))\
					.scalar() or 0
				page = before // per_page + 1
		page = min(max(page, 1), pages)
		job_ids = [row.job_id for row in
		           db.query(DeviceResult.job_id, done).filter(where)
		           .group_by(DeviceResult.job_id)
		           .order_by(done.desc(), DeviceResult.job_id.desc())
		           .limit(per_page).offset((page - 1) * per_page)]
		results = db.query(DeviceResult).filter(
			where, DeviceResult.job_id.in_(job_ids)).all() if job_ids else []
		return JobPage(results=results, total=total, page=page, pages=pages)

	def device_labels(self, results: Iterable[DeviceResult]) -> dict[Target, str]:
		"""(ip, port) → label for naming the devices of these results: an
		admin's from anyone's inventory (only these devices are read; an empty
		label: the IP), anyone else's from the devices they see."""
		view = InventoryView(self.session, self.viewer)
		if self.viewer.is_admin:
			return view.endpoint_labels(
				LabelScope.ANYONE, {(r.device_ip, r.device_port) for r in results})
		return view.endpoint_labels(LabelScope.VISIBLE)

	def metadata(self, job_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, JobMetadata]:
		""":returns: the jobs' metadata (comment, commands), by job id"""
		ids = set(job_ids)
		rows = self.session.query(JobMetadata).filter(
			JobMetadata.job_id.in_(ids)).all() if ids else []
		return {m.job_id: m for m in rows}

	def owner_names(self, results: Iterable[DeviceResult]) -> dict[uuid.UUID, str]:
		""":returns: user id → username, for these results' owners"""
		owner_ids = {r.user_id for r in results}
		return {u.id: u.username for u in self.session.query(User)
		        .filter(User.id.in_(owner_ids))} if owner_ids else {}

	# ── one job ──

	def summary(self, job_id: uuid.UUID) -> dict[str, Any] | None:
		"""A finished job in a few lines (the completion card): job_id,
		comment, status, device_count, counts by status, action_needed
		[{device, text}].

		:returns: the summary; None until its results are stored, or when
		 the viewer may not see it"""
		rows = self.session.query(DeviceResult).filter_by(job_id=job_id).all()
		if not rows or not self.may_see(rows[0].user_id):
			return None
		# an admin names devices as on Results (anyone's inventory)
		labels = self.device_labels(rows)
		meta = self.session.query(JobMetadata).filter_by(job_id=job_id).first()
		counts: dict[str, int] = {}
		for r in rows:
			counts[r.status] = counts.get(r.status, 0) + 1
		return {"job_id": str(job_id), "comment": meta.comment if meta else None,
		        "status": job_status(rows), "device_count": len(rows), "counts": counts,
		        "action_needed": [{"device": device_label(labels, r), "text": r.action_needed}
		                          for r in rows if r.action_needed]}

	def config_snapshot(self, job_id: uuid.UUID, ip: str,
	                    port: int | None = None) -> ConfigSnapshot | None:
		"""A device's fetched config in a job (Verify Diff).

		:param port: picks one of the devices sharing the IP in the job;
		 None: the first one
		:returns: the snapshot and the job's commands; None when there's no
		 such device in the job, or the viewer may not see the job"""
		filters: dict[str, Any] = {"job_id": job_id, "device_ip": ip}
		if port is not None:
			filters["device_port"] = port
		row = self.session.query(DeviceResult).filter_by(**filters).first()
		if not row or not self.may_see(row.user_id):
			return None
		if row.fetched_config is None:
			return ConfigSnapshot(config=None, commands=[], device_type=row.device_type)
		meta = self.session.query(JobMetadata).filter_by(job_id=job_id).first()
		return ConfigSnapshot(config=row.fetched_config,
		                      commands=meta.commands if meta else [],
		                      device_type=row.device_type)

	def successful_endpoints(self, job_id: uuid.UUID, owner_id: uuid.UUID) -> set[Target]:
		""":returns: the ip:port of the job's devices configured successfully
		 (what a rollback targets)"""
		rows = self.session.query(DeviceResult.device_ip, DeviceResult.device_port).filter_by(
			user_id=owner_id, job_id=job_id, status=DeviceStatus.SUCCESS.value).all()
		return {(ip, port) for ip, port in rows}

	# ── all time ──

	def totals(self, user_id: uuid.UUID) -> dict[str, Any]:
		"""A user's rollouts in numbers, all time (the Account page).

		:returns: total_rollouts (jobs), total_devices (device results),
		 success_rate (% of the jobs job_status calls success - one failed
		 device spoils it, as on Results; None without a job),
		 most_common_platform (None without results), total_commands"""
		rows = self.session.query(DeviceResult).filter(DeviceResult.user_id == user_id).all()
		by_job: dict[uuid.UUID, list[DeviceResult]] = defaultdict(list)
		for r in rows:
			by_job[r.job_id].append(r)
		successful = sum(1 for job in by_job.values() if job_status(job) == DeviceStatus.SUCCESS)
		return {
			"total_rollouts": len(by_job),
			"total_devices": len(rows),
			"success_rate": round(successful / len(by_job) * 100) if by_job else None,
			"most_common_platform": (Counter(r.device_type for r in rows).most_common(1)[0][0]
			                         if rows else None),
			"total_commands": sum(r.commands_sent for r in rows),
		}

	# ── the last 30 days ──

	def recent(self, user_id: uuid.UUID | None) -> list[DeviceResult]:
		""":param user_id: whose device results; None: everyone's
		:returns: the device results started in the last RECENT_DAYS days"""
		cutoff = datetime.now() - timedelta(days=RECENT_DAYS)
		query = self.session.query(DeviceResult).filter(DeviceResult.started_at >= cutoff)
		if user_id is not None:
			query = query.filter(DeviceResult.user_id == user_id)
		return query.all()

	def matching(self, user_id: uuid.UUID,
	             condition: ColumnElement[bool]) -> list[DeviceResult]:
		""":returns: the user's device results that match the query builder's
		 condition, newest first, QUERY_LIMIT at most"""
		return (self.session.query(DeviceResult)
		        .filter(DeviceResult.user_id == user_id).filter(condition)
		        .order_by(DeviceResult.started_at.desc()).limit(QUERY_LIMIT).all())
