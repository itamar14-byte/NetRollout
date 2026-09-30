# python utilities
import uuid

# services
# flask
from flask import (Blueprint, render_template, request, current_app, redirect,
                   url_for, flash)
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

# local modules
from src.db.tables import User
from src.passwords import temporary_password
from src.webapp.utils import ok, err, require_admin, end_user_sessions

bp = Blueprint('admin_users', __name__, url_prefix='/admin')


##############################Route Helpers####################################
def user_action_factory(user, action, db_session):
	if action == "approve":
		user.is_approved = True
		user.is_active = True
	elif action == "enable":
		user.is_active = True
	elif action == "disable":
		user.is_active = False
	elif action == "promote":
		user.role = "admin"
		user.is_approved = True
		user.is_active = True
	elif action == "demote":
		user.role = "user"
	elif action == "delete":
		db_session.delete(user)
	elif action == "reset_2fa":
		# They enroll a new authenticator at their next sign-in (start_otp_flow
		# sends users without a secret to enrollment). Local users only —
		# LDAP users and the factory admin don't use 2FA.
		user.otp_secret = None
	elif action == "terminate_session":
		end_user_sessions(user.id)


##############################Routes#######################################
@bp.route("")
@login_required
@require_admin
def admin_panel():
	return redirect(url_for("admin_users.admin_users"))


@bp.route("/users")
@login_required
@require_admin
def admin_users():
	with current_app.backend.postgres.get_session() as db_session:
		users = db_session.query(User).order_by(User.created_at).all()
		db_session.expunge_all()

	session_user_ids = {
		redis_key.decode().replace("user_session:", "")
		for redis_key in
		current_app.backend.redis.client.scan_iter("user_session:*")
	}
	return render_template("admin_users.html", users=users,
	                       active_section="users",
	                       session_user_ids=session_user_ids)


@bp.route("/users/<uuid:user_id>/<action>", methods=["POST"])
@login_required
@require_admin
def admin_user_action(user_id, action):
	if action in ("disable", "delete") and user_id == current_user.id:
		flash("You cannot perform this action on your own account.", "danger")
		return redirect(url_for("admin_users.admin_users"))
	with current_app.backend.postgres.get_session() as db_session:
		user = db_session.query(User).filter_by(id=user_id).first()

		if not user or user.username == "admin":
			return redirect(url_for("admin_users.admin_users"))

		target_username = user.username
		target_id = user.id
		user_action_factory(user, action, db_session)
	current_app.web.audit(f"user.{action}", object_type="User",
	                      object_id=target_id, object_label=target_username)
	return redirect(url_for("admin_users.admin_users"))


@bp.route("/users/<uuid:user_id>/reset_password", methods=["POST"])
@login_required
@require_admin
def admin_reset_password(user_id):
	"""A temporary password for another local user, returned once for the
	admin to hand over; the user must choose their own at the next sign-in
	(must_change_password) and every session of theirs ends now. Not for LDAP
	users (the directory owns it), the admin's own account (use Change
	password) or the factory admin."""
	if user_id == current_user.id:
		return err("Use Change password for your own account", 400)
	with current_app.backend.postgres.get_session() as db_session:
		user = db_session.get(User, user_id)
		if not user:
			return err("User not found", 404)
		if user.username == "admin":
			return err("The factory admin's password can't be reset", 400)
		if user.auth_type != "local":
			return err("LDAP passwords are managed in the directory", 400)
		temporary = temporary_password(user.username)
		user.password_hash = generate_password_hash(temporary)
		user.must_change_password = True
		username = user.username
	ended = end_user_sessions(user_id)
	current_app.web.audit("user.reset_password", object_type="User",
	                      object_id=user_id, object_label=username,
	                      detail={"sessions_ended": ended})
	return ok(username=username, temporary_password=temporary)


@bp.route("/users/bulk/<action>", methods=["POST"])
@login_required
@require_admin
def admin_bulk_action(action):
	raw = request.form.get("user_ids", "")
	try:
		user_ids = [uuid.UUID(uid.strip()) for uid in raw.split(",") if
		            uid.strip()]
	except ValueError:
		return redirect(url_for("admin_users.admin_users"))
	affected = []
	with current_app.backend.postgres.get_session() as db_session:
		for uid in user_ids:
			user = db_session.get(User, uid)
			if not user or user.username == "admin":
				continue
			if action in ("disable", "delete") and uid == current_user.id:
				continue
			affected.append(user.username)
			user_action_factory(user, action, db_session)
	current_app.web.audit(f"user.bulk_{action}",
	                      detail={"count": len(affected), "users": affected})
	return redirect(url_for("admin_users.admin_users"))


@bp.route("/sessions")
@login_required
@require_admin
def admin_sessions():
	sessions = []
	keys = list(current_app.backend.redis.client.scan_iter("user_session:*"))
	if keys:
		with current_app.backend.postgres.get_session() as db_session:
			for k in keys:
				user_id_str = k.decode().replace("user_session:", "")
				try:
					uid = uuid.UUID(user_id_str)
				except ValueError:
					continue
				user = db_session.query(User).filter_by(id=uid).first()
				if not user:
					continue
				ttl = current_app.backend.redis.client.ttl(k)
				elapsed = max(0, 86400 - ttl) if ttl > 0 else 0
				sessions.append({
					"user_id": user_id_str,
					"username": user.username,
					"auth_type": user.auth_type,
					"role": user.role,
					"elapsed_secs": elapsed,
				})
			db_session.expunge_all()

	return render_template("live_sessions.html",
	                       local_sessions=[s for s in sessions if
	                                       s["auth_type"] == "local"],
	                       ldap_sessions=[s for s in sessions if
	                                      s["auth_type"] == "ldap"],
	                       active_section="sessions")


@bp.route("/sessions/<uuid:user_id>/kick", methods=["POST"])
@login_required
@require_admin
def admin_sessions_kick(user_id):
	sid = current_app.backend.redis.client.get(f"user_session:{user_id}")
	if not sid:
		return err("Session not found", 404)
	current_app.backend.redis.client.delete(f"redis_session:{sid.decode()}")
	current_app.backend.redis.client.delete(f"user_session:{user_id}")
	current_app.web.audit("admin.session_kick", object_type="User",
	                      object_id=user_id,
	                      success=True)
	return ok()
