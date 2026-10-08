"""The audit log (/admin/audit): who did what and when, newest first, filtered
by actor, action and outcome - for admins."""
from flask import Blueprint, render_template, request
from flask_login import login_required

from src.db.tables import AuditLog
from src.webapp.app import current_app
from src.webapp.http import require_admin


bp = Blueprint('admin_audit', __name__, url_prefix='/admin')


@bp.route("/audit")
@login_required
@require_admin
def admin_audit() -> str:
	"""The audit log, newest first (500 at most), filtered by ?actor (part
	of the name), ?action, ?success."""
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
