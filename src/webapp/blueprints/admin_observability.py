# python utilities
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

# services
# flask
from flask import Blueprint, render_template, request, current_app, jsonify
from flask_login import login_required

# local modules
from src.db.tables import AuditLog, DeviceResult, User, Inventory
from src.webapp.utils import (ok, err, require_admin, with_json,
                              compile_query_rules, QUERY_AUDIT_LOG_FIELDS,
                              AUDIT_LOG_COLUMNS)

bp = Blueprint('admin_observability', __name__, url_prefix='/admin')


##############################Route Helpers#####################################
##############################Routes###########################################
@bp.route("/audit")
@login_required
@require_admin
def admin_audit():
	filter_actor = request.args.get("actor", "").strip()
	filter_action = request.args.get("action", "").strip()
	filter_success = request.args.get("success", "")

	with current_app.backend.postgres.get_session() as db_session:
		q = db_session.query(AuditLog).order_by(AuditLog.timestamp.desc())
		if filter_actor:
			q = q.filter(AuditLog.actor_username.ilike(f"%{filter_actor}%"))
		if filter_action:
			q = q.filter(AuditLog.action == filter_action)
		if filter_success == "true":
			q = q.filter(AuditLog.success == True)
		elif filter_success == "false":
			q = q.filter(AuditLog.success == False)
		entries = q.limit(500).all()
		distinct_actions = [r[0] for r in
		                    db_session.query(AuditLog.action).distinct()
		                    .order_by(AuditLog.action).all()]
		db_session.expunge_all()

	return render_template("admin_audit.html",
	                       entries=entries,
	                       distinct_actions=distinct_actions,
	                       filter_actor=filter_actor,
	                       filter_action=filter_action,
	                       filter_success=filter_success,
	                       active_section="audit")


@bp.route("/analytics")
@login_required
@require_admin
def admin_analytics():
	with current_app.backend.postgres.get_session() as db_session:
		cutoff = datetime.now() - timedelta(days=30)
		results_30d = db_session.query(DeviceResult).filter(
			DeviceResult.started_at >= cutoff
		).all()

		all_users = db_session.query(User).order_by(User.username).all()
		total_users = len(all_users)
		active_user_ids = {r.user_id for r in results_30d}
		total_ops = len(results_30d)
		total_jobs = len({r.job_id for r in results_30d})
		success_count = sum(1 for r in results_30d if r.status == "success")

		org_kpi = {
			"active_users": len(active_user_ids),
			"total_users": total_users,
			"total_jobs": total_jobs,
			"total_ops": total_ops,
			"success_rate": round(
				success_count / total_ops * 100) if total_ops else None,
		}

		user_stats = defaultdict(
			lambda: {"job_ids": set(), "devices": 0, "last_job_at": None})
		for r in results_30d:
			s = user_stats[r.user_id]
			s["job_ids"].add(r.job_id)
			s["devices"] += 1
			if s["last_job_at"] is None or r.completed_at > s["last_job_at"]:
				s["last_job_at"] = r.completed_at

		username_map = {u.id: u.username for u in all_users}
		active_users_rows = sorted(
			[
				{
					"username": username_map.get(uid, str(uid)),
					"job_count": len(s["job_ids"]),
					"devices_reached": s["devices"],
					"last_job_at": s["last_job_at"],
				}
				for uid, s in user_stats.items()
			],
			key=lambda x: x["job_count"],
			reverse=True,
		)[:10]

		fail_counts = defaultdict(lambda: {"device_type": "", "count": 0})
		for r in results_30d:
			if r.status == "failed":
				fail_counts[r.device_ip]["device_type"] = r.device_type
				fail_counts[r.device_ip]["count"] += 1

		failed_ips = set(fail_counts.keys())
		inv_rows = (
			db_session.query(Inventory.ip, Inventory.label)
			.filter(Inventory.ip.in_(failed_ips))
			.all()
			if failed_ips else []
		)
		label_map = {row.ip: row.label for row in inv_rows}

		failed_devices_rows = sorted(
			[
				{
					"ip": ip,
					"label": label_map.get(ip),
					"device_type": s["device_type"],
					"fail_count": s["count"],
				}
				for ip, s in fail_counts.items()
			],
			key=lambda x: x["fail_count"],
			reverse=True,
		)[:10]

		db_session.expunge_all()

	return render_template(
		"admin_analytics.html",
		org_kpi=org_kpi,
		active_users=active_users_rows,
		all_users=[{"id": str(u.id), "username": u.username} for u in
		           all_users],
		failed_devices=failed_devices_rows,
		active_section="analytics",
	)


@bp.route("/analytics/query", methods=["POST"])
@login_required
@require_admin
@with_json()
def admin_analytics_query(data):
	try:
		rules = data.get("rules", [])
		filters = compile_query_rules(rules, QUERY_AUDIT_LOG_FIELDS)
	except (ValueError, KeyError) as e:
		return err(str(e))

	with current_app.backend.postgres.get_session() as db_session:
		query = db_session.query(AuditLog).filter(filters)

		rows_raw = query.order_by(AuditLog.timestamp.desc()).limit(
			200).all()
		columns = AUDIT_LOG_COLUMNS
		rows = [{col: getattr(r, col) for col in columns} for r in rows_raw]
	parsed_rows = [{col: v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v,
	                                                                   datetime)
	else str(v) if isinstance(v, uuid.UUID)
	else v
	                for col, v in row.items()}
	               for row in rows]
	return jsonify({"columns": columns, "rows": parsed_rows})


@bp.route("/active_job_count")
@login_required
@require_admin
def admin_active_job_count():
	count = int(
		current_app.backend.redis.client.get("netrollout:active_count") or 0)
	return ok(count=count)
