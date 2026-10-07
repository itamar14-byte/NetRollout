"""Admin → Users and Live Sessions: approve, enable / disable, promote /
demote, delete, reset 2FA or a password, end a user's sessions, add a user;
the sessions signed in now. Admins only; every action audited."""
import time
import uuid
from typing import Any

from flask import Blueprint, Response, render_template, request, redirect, url_for, flash
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from src.db.tables import User
from src.passwords import temporary_password
from src.webapp.accounts import AccountError, new_local_user, pending_requests
from src.webapp.extensions import signed_in_users
from src.webapp.flask_app import current_app
from src.webapp.utils import ok, err, require_admin, end_user_sessions, with_json

bp = Blueprint('admin_users', __name__, url_prefix='/admin')


@bp.app_context_processor
def pending_access_requests() -> dict[str, Any]:
	"""The admin sidebar's count of access requests waiting (none: no badge)."""
	if not (request.path.startswith("/admin") and current_user.is_authenticated
	        and current_user.role == "admin"):
		return {}
	try:
		with current_app.backend.postgres.get_session() as db_session:
			return {"pending_requests": pending_requests(db_session)}
	except SQLAlchemyError:
		return {}


##############################Route Helpers####################################
def user_action_factory(user: User, action: str, db_session: Session) -> None:
	"""Apply one admin action to a user (in the caller's session).

	:param action: approve / enable / disable / promote / demote / delete /
	 reset_2fa / terminate_session - anything else does nothing"""
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
		user.role = "operator"
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
def admin_panel() -> ResponseReturnValue:
	""":returns: the way to the admin panel's first page (Users)"""
	return redirect(url_for("admin_users.admin_users"))


@bp.route("/users")
@login_required
@require_admin
def admin_users() -> str:
	"""The Users page: every account, and who has a session."""
	with current_app.backend.postgres.get_session() as db_session:
		users = db_session.query(User).order_by(User.created_at).all()
		db_session.expunge_all()

	return render_template("admin_users.html", users=users,
	                       active_section="users",
	                       session_user_ids=set(signed_in_users()))


@bp.route("/users/<uuid:user_id>/<action>", methods=["POST"])
@login_required
@require_admin
def admin_user_action(user_id: uuid.UUID, action: str) -> ResponseReturnValue:
	"""One action on one user (user_action_factory), audited as
	user.<action>. Not on the factory admin; an admin can't disable or
	delete their own account."""
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
def admin_reset_password(user_id: uuid.UUID) -> ResponseReturnValue:
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


@bp.route("/users/new", methods=["POST"])
@login_required
@require_admin
@with_json()
def admin_add_user(data: dict[str, Any]) -> ResponseReturnValue:
	"""An admin's Add user: Request access and Approve in one step - an
	approved, active local account with a temporary password, shown once,
	which the user must replace at the first sign-in (as after a reset; the
	admin never knows the password in use). 2FA is enrolled at that sign-in,
	as for every local user."""
	username = str(data.get("username", "")).strip()
	role = str(data.get("role", "operator"))
	temporary = temporary_password(username)
	with current_app.backend.postgres.get_session() as db_session:
		try:
			user = new_local_user(db_session, username=username, email=str(data.get("email", "")),
			                      full_name=str(data.get("full_name", "")),
			                      position=str(data.get("position", "")), password=temporary,
			                      role=role, approved=True, must_change_password=True)
		except AccountError as e:
			db_session.rollback()
			return err(str(e), 422)
		except IntegrityError:
			db_session.rollback()
			return err("That username or email address is already in use.", 409)
		user_id, username = user.id, user.username
	current_app.web.audit("user.created", object_type="User", object_id=user_id,
	                      object_label=username, detail={"role": role})
	return ok(username=username, role=role, temporary_password=temporary)


@bp.route("/users/bulk/<action>", methods=["POST"])
@login_required
@require_admin
def admin_bulk_action(action: str) -> ResponseReturnValue:
	"""One action on the users ticked (form user_ids, comma separated) - the
	factory admin and, for disable / delete, the admin's own account
	skipped. One audit row: user.bulk_<action> with the names."""
	raw = request.form.get("user_ids", "")
	try:
		user_ids = [uuid.UUID(uid.strip()) for uid in raw.split(",") if
		            uid.strip()]
	except ValueError:
		return redirect(url_for("admin_users.admin_users"))
	affected: list[str] = []
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
def admin_sessions() -> str:
	"""Live Sessions: the users signed in now (a session within its limits),
	local and LDAP apart, with how long ago their newest sign-in was."""
	now = time.time()
	signed_in = signed_in_users(now)
	sessions: list[dict[str, Any]] = []
	if signed_in:
		with current_app.backend.postgres.get_session() as db_session:
			users = db_session.query(User).filter(
				User.id.in_({uuid.UUID(uid) for uid in signed_in})).all()
			for user in users:
				sessions.append({
					"user_id": str(user.id),
					"username": user.username,
					"auth_type": user.auth_type,
					"role": user.role,
					"elapsed_secs": max(0, int(now - signed_in[str(user.id)])),
				})

	return render_template("live_sessions.html",
	                       local_sessions=[s for s in sessions if
	                                       s["auth_type"] == "local"],
	                       ldap_sessions=[s for s in sessions if
	                                      s["auth_type"] == "ldap"],
	                       active_section="sessions")


@bp.route("/sessions/<uuid:user_id>/kick", methods=["POST"])
@login_required
@require_admin
def admin_sessions_kick(user_id: uuid.UUID) -> ResponseReturnValue:
	"""Sign the user out everywhere - every session of theirs, on every
	computer (as Terminate Session). An open page notices within 30 s.

	:returns: ok, or 404 when they have no session"""
	ended = end_user_sessions(user_id)
	if not ended:
		return err("Session not found", 404)
	current_app.web.audit("admin.session_kick", object_type="User",
	                      object_id=user_id, success=True,
	                      detail={"sessions_ended": ended})
	return ok()
