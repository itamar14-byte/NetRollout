"""The audit log (/admin/audit): who did what and when, newest first, filtered
by actor, action, outcome and day - for admins; and the same as a CSV file."""
import json
from datetime import datetime, time, timedelta
from typing import Any

from flask import Blueprint, render_template, request
from flask.typing import ResponseReturnValue
from flask_login import login_required
from sqlalchemy.orm import Query, Session

from src.audit import AuditAction
from src.db.tables import AuditLog
from src.webapp.app import current_app
from src.webapp.csv_download import csv_download
from src.webapp.http import date_arg, require_admin


bp = Blueprint('admin_audit', __name__, url_prefix='/admin')

# the most entries the page shows (the export has them all)
PAGE_LIMIT = 500
EXPORT_COLUMNS = ["time", "actor", "action", "object_type", "object", "outcome", "ip", "detail"]


def filter_args() -> dict[str, str]:
	""":returns: the filters asked for (?actor: part of the name, ?action,
	 ?success: true / false, ?from and ?to: days), as the page shows them -
	 every key, blank when not set"""
	return {"actor": request.args.get("actor", "").strip(),
	        "action": request.args.get("action", "").strip(),
	        "success": request.args.get("success", ""),
	        "from": request.args.get("from", "").strip(),
	        "to": request.args.get("to", "").strip()}


def filtered(db_session: Session) -> Query[AuditLog]:
	""":returns: the entries the request's filters (filter_args) let
	 through, newest first"""
	args = filter_args()
	q = db_session.query(AuditLog).order_by(AuditLog.timestamp.desc())
	if args["actor"]:
		q = q.filter(AuditLog.actor_username.ilike(f"%{args['actor']}%"))
	if args["action"]:
		q = q.filter(AuditLog.action == args["action"])
	if args["success"] == "true":
		q = q.filter(AuditLog.success == True)
	elif args["success"] == "false":
		q = q.filter(AuditLog.success == False)
	since, until = date_arg("from"), date_arg("to")
	if since is not None:
		q = q.filter(AuditLog.timestamp >= datetime.combine(since, time()))
	if until is not None:
		q = q.filter(AuditLog.timestamp < datetime.combine(until + timedelta(days=1), time()))
	return q


@bp.route("/audit")
@login_required
@require_admin
def admin_audit() -> str:
	"""The audit log, newest first (PAGE_LIMIT at most), filtered as
	filter_args says."""
	with current_app.backend.postgres.get_session() as db_session:
		entries = filtered(db_session).limit(PAGE_LIMIT).all()
		distinct_actions = [r[0] for r in
		                    db_session.query(AuditLog.action).distinct()
		                    .order_by(AuditLog.action).all()]
		db_session.expunge_all()

	args = filter_args()
	return render_template("admin_audit.html",
	                       entries=entries,
	                       distinct_actions=distinct_actions,
	                       filter_actor=args["actor"],
	                       filter_action=args["action"],
	                       filter_success=args["success"],
	                       filter_from=args["from"],
	                       filter_to=args["to"],
	                       page_limit=PAGE_LIMIT,
	                       active_section="audit")


@bp.route("/audit/export.csv")
@login_required
@require_admin
def admin_audit_export() -> ResponseReturnValue:
	"""The audit log as a CSV file: every entry the page's filters let
	through, not only the PAGE_LIMIT shown. Audited with the filters (after
	the export, so it isn't in its own file)."""
	with current_app.backend.postgres.get_session() as db_session:
		rows: list[list[Any]] = [
			[e.timestamp.strftime("%Y-%m-%d %H:%M:%S"), e.actor_username, e.action,
			 e.object_type, e.object_label or (str(e.object_id) if e.object_id else None),
			 "ok" if e.success else "fail", e.ip_address,
			 json.dumps(e.detail, ensure_ascii=False, separators=(", ", ": "))
			 if e.detail else None]
			for e in filtered(db_session)]
	current_app.web.audit(AuditAction.AUDIT_EXPORTED, detail={
		**{key: value for key, value in filter_args().items() if value}, "rows": len(rows)})
	return csv_download("audit", EXPORT_COLUMNS, rows)
