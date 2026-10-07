"""Analytics: the user's last 30 days in numbers, and the query builder over
their device results (an admin may look at any user's)."""
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from flask import Blueprint, render_template, request, jsonify, Response
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy import ColumnElement, and_, or_

from src.db.tables import DeviceResult, Inventory, User, AuditLog
from src.jobs import build_kpi
from src.webapp.app import current_app
from src.webapp.http import err, with_json, ok, require_admin


bp = Blueprint('analytics', __name__, url_prefix='/analytics')

##############################Constants#######################################

QUERY_DEVICE_RESULT_FIELDS = {
	"started_at": (
		DeviceResult.started_at,
		{"equal", "less_or_equal", "greater_or_equal"}),
	"device_type": (
		DeviceResult.device_type, {"equal", "not_equal"}),
	"status": (
		DeviceResult.status, {"equal", "not_equal"}),
	"commands_sent": (
		DeviceResult.commands_sent,
		{"equal", "not_equal", "greater_or_equal",
		 "less_or_equal"}),
	"device_port": (
		DeviceResult.device_port, {"equal", "not_equal"}),
	"device_ip": (
		DeviceResult.device_ip, {"equal", "contains", "begins_with"}),
}
DEVICE_RESULT_COLUMNS = ["job_id", "device_ip", "device_port", "device_type",
                         "status",
                         "commands_sent", "commands_verified",
                         "started_at", "completed_at"]

##############################Routes#######################################
@bp.route("")
@login_required
def analytics() -> str:
	"""The analytics page: KPIs and top platforms over 30 days, for the user
	- or, for an admin, ?user=<id>."""
	selected_user = "me"
	scope_user_id = current_user.id

	if current_user.role == "admin":
		param = request.args.get("user", "me").strip()
		if param != "me":
			try:
				scope_user_id = uuid.UUID(param)
				selected_user = param
			except ValueError:
				pass

	with current_app.backend.postgres.get_session() as db_session:
		cutoff = datetime.now() - timedelta(days=30)
		results_30d = db_session.query(DeviceResult).filter(
			DeviceResult.started_at >= cutoff,
			DeviceResult.user_id == scope_user_id,
		).all()

		inv_label_map = {
			row.ip: row.label
			for row in db_session.query(Inventory.ip, Inventory.label)
			.filter(Inventory.user_id == scope_user_id).all()
		}
		users = db_session.query(User).order_by(User.username).all() \
			if current_user.role == "admin" else []
		db_session.expunge_all()

	kpi = build_kpi(results_30d, inv_label_map)
	kpi["top_platforms"] = Counter(r.device_type for r in
	                               results_30d).most_common(3)

	selected_username = next(
		(u.username for u in users if str(u.id) == selected_user), selected_user
	) if selected_user != "me" else "me"

	return render_template("analytics.html",
	                       kpi=kpi,
	                       users=users,
	                       selected_user=selected_user,
	                       selected_username=selected_username,
	                       active_section="analytics")


@bp.route("/query", methods=["POST"])
@login_required
@with_json()
def analytics_query(data: dict[str, Any]) -> ResponseReturnValue:
	"""The query builder's rules → the matching device results (200 at most,
	newest first): JSON {rules, user?}.

	:returns: {columns, rows} or an error (a field or operator not allowed)"""
	scope_user_id = current_user.id
	if current_user.role == "admin":
		param = data.get("user", "me").strip()
		if param != "me":
			try:
				scope_user_id = uuid.UUID(param)
			except ValueError:
				pass
	try:
		rules = data.get("rules", [])
		filters = compile_query_rules(rules, QUERY_DEVICE_RESULT_FIELDS)
	except (ValueError, KeyError) as e:
		return err(str(e))
	with current_app.backend.postgres.get_session() as db_session:
		query = db_session.query(DeviceResult).filter(
			DeviceResult.user_id == scope_user_id).filter(filters)

		rows_raw = query.order_by(DeviceResult.started_at.desc()).limit(
			200).all()
		columns = DEVICE_RESULT_COLUMNS
		rows = [{col: getattr(r, col) for col in columns} for r in rows_raw]
	return jsonify({"columns": columns, "rows": _json_rows(rows)})


QUERY_OPS: dict[str, Callable[[Any, Any], ColumnElement[bool]]] = {
	"equal": lambda x, y: x == y,
	"not_equal": lambda x, y: x != y,
	"greater_or_equal": lambda x, y: x >= y,
	"less_or_equal": lambda x, y: x <= y,
	"contains": lambda x, y: x.ilike(f"%{y}%"),
	"begins_with": lambda x, y: x.ilike(f"{y}%"),
	"ends_with": lambda x, y: x.ilike(f"%{y}")
}


#######################Query helpers###############################
def _json_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
	""":returns: the query's rows for JSON - a time as "YYYY-MM-DD HH:MM:SS",
	 an id as text, the rest as it is"""
	return [{col: v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime)
	         else str(v) if isinstance(v, uuid.UUID)
	         else v
	         for col, v in row.items()}
	        for row in rows]


def compile_query_rules(node: dict[str, Any],
                        allowed_fields: dict[str, tuple[Any, set[str]]]) -> ColumnElement[bool]:
	"""A jQuery QueryBuilder tree as an SQL filter. Each node is either
	 - a GROUP: {"condition": "AND"/"OR", "rules": [...child nodes...]}
	 - a LEAF: {"field": "status", "operator": "equal", "value": "success"}

	:param allowed_fields: the fields that may be filtered on → (their
	 column, the operators allowed on it) - the only columns that reach SQL
	:raises ValueError: a field or operator not allowed, or a bad date
	:raises KeyError: a node without its keys"""
	if "condition" in node:
		combinator = and_ if node["condition"] == "AND" else or_
		return combinator(*[compile_query_rules(r, allowed_fields) for r in
		                    node["rules"]])
	field_name = node["field"]
	operator = node["operator"]
	value = node["value"]

	if field_name not in allowed_fields:
		raise ValueError(f"Field not allowed: {field_name}")

	column, allowed_ops = allowed_fields[field_name]
	if operator not in allowed_ops:
		raise ValueError(
			f"Operator {operator} not allowed for field {field_name}")

	# DateTime columns need a Python datetime object, not a raw string
	if hasattr(column, "type") and column.type.__class__.__name__ == 'DateTime':
		try:
			value = datetime.strptime(value, "%Y-%m-%d")
		except (ValueError, TypeError):
			raise ValueError(f"Invalid date: {value}")

	# Boolean columns: QueryBuilder sends string keys ("true"/"false")
	if hasattr(column, "type") and column.type.__class__.__name__ == 'Boolean':
		if isinstance(value, str):
			value = value.lower() == "true"

	return QUERY_OPS[operator](column, value)


# ══ The organisation's analytics, for admins: /admin/analytics ══════════════

admin_bp = Blueprint('admin_observability', __name__, url_prefix='/admin')


##############################Constants#####################################
QUERY_AUDIT_LOG_FIELDS = {
	"timestamp": (
		AuditLog.timestamp, {"equal", "less_or_equal", "greater_or_equal"}),
	"actor_username": (
		AuditLog.actor_username,
		{"equal", "not_equal", "contains", "begins_with"}),
	"action": (
		AuditLog.action, {"equal", "not_equal", "contains", "begins_with"}),
	"object_type": (
		AuditLog.object_type, {"equal", "not_equal"}),
	"success": (
		AuditLog.success, {"equal"}),
	"ip_address": (
		AuditLog.ip_address, {"equal", "contains", "begins_with"}),
}

AUDIT_LOG_COLUMNS = ["timestamp", "actor_username", "action",
                     "object_type",
                     "object_label", "success", "ip_address"]


##############################Routes###########################################
@admin_bp.route("/analytics")
@login_required
@require_admin
def admin_analytics() -> str:
	"""The organisation's last 30 days: KPIs, the 10 most active users, the
	10 devices that failed most."""
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

		user_stats: defaultdict[uuid.UUID, dict[str, Any]] = defaultdict(
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

		fail_counts: defaultdict[str, dict[str, Any]] = defaultdict(
			lambda: {"device_type": "", "count": 0})
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


@admin_bp.route("/analytics/query", methods=["POST"])
@login_required
@require_admin
@with_json()
def admin_analytics_query(data: dict[str, Any]) -> ResponseReturnValue:
	"""The query builder's rules → the matching audit rows (200 at most,
	newest first): JSON {rules}.

	:returns: {columns, rows} or an error (a field or operator not allowed)"""
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
	return jsonify({"columns": columns, "rows": _json_rows(rows)})


@admin_bp.route("/active_job_count")
@login_required
@require_admin
def admin_active_job_count() -> Response:
	"""Rollouts running and queued, from the orchestrator's memory: exact,
	and works while Redis is down.

	:returns: {count, running, queued}"""
	counts = current_app.orchestrator.counts()
	return ok(count=counts["running"] + counts["queued"], **counts)
