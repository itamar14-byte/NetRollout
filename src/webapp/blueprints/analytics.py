# python utilities
import uuid
from collections import Counter
from datetime import datetime, timedelta

# services
# flask
from flask import Blueprint, render_template, current_app, request, jsonify
from flask_login import current_user, login_required

# local modules
from src.db.tables import DeviceResult, Inventory, User
from src.webapp.utils import (err, with_json, build_kpi,
                              compile_query_rules)

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
	"device_ip": (
		DeviceResult.device_ip, {"equal", "contains", "begins_with"}),
}
DEVICE_RESULT_COLUMNS = ["job_id", "device_ip", "device_type",
                         "status",
                         "commands_sent", "commands_verified",
                         "started_at", "completed_at"]

##############################Routes#######################################
@bp.route("")
@login_required
def analytics():
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
def analytics_query(data):
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
	parsed_rows = [{col: v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v,
	                                                                   datetime)
	else str(v) if isinstance(v, uuid.UUID)
	else v
	                for col, v in row.items()}
	               for row in rows]
	return jsonify({"columns": columns, "rows": parsed_rows})
