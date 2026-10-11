"""Admin → Users and Live Sessions: approve, enable / disable, promote /
demote, delete, reset 2FA or a password, end a user's sessions, add a user;
the sessions signed in now. Admins only; every action audited."""
import time
import uuid
from typing import Any

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash

from src.accounts.users import (AccountError, Accounts, clear_lockout, is_factory_admin,
                                temporary_password)
from src.audit import AuditAction
from src.db.tables import User, AuthType, Role
from src.webapp.app import current_app
from src.webapp.http import ok, err, require_admin, with_json, viewer

bp = Blueprint('admin_users', __name__, url_prefix='/admin')

# what user_action_factory does - anything else is 404, and never audited
USER_ACTIONS = frozenset({"approve", "enable", "disable", "promote", "demote",
                          "delete", "reset_2fa", "terminate_session"})
# the actions an admin may take on the factory admin: it can be disabled (an
# organisation signing in through LDAP or its own admins) and enabled again,
# its 2FA turned off - never deleted, demoted or signed out by another
FACTORY_ACTIONS = frozenset({"enable", "disable", "reset_2fa"})
# the actions that take an administrator away (Accounts.check_admins_left)
REMOVES_ADMIN = frozenset({"disable", "demote", "delete"})
# the factory admin turns its own 2FA off on My Account, proved by its
# password and a code - never by a Reset 2FA on itself here
OWN_2FA = "Turn your own two-factor sign-in off on My Account - it asks for your password and a code."


def _own_factory_2fa(user: User, action: str) -> bool:
	""":returns: whether this is the signed-in factory admin resetting its own 2FA"""
	return action == "reset_2fa" and is_factory_admin(user) and user.id == current_user.id


@bp.app_context_processor
def pending_access_requests() -> dict[str, Any]:
	"""The admin sidebar's count of access requests waiting (none: no badge)."""
	if not (request.path.startswith("/admin") and current_user.is_authenticated
	        and viewer().is_admin):
		return {}
	try:
		with current_app.backend.postgres.get_session() as db_session:
			return {"pending_requests": Accounts(db_session).pending_count()}
	except SQLAlchemyError:
		return {}


def user_action_factory(user: User, action: str, db_session: Session) -> None:
	"""Apply one admin action to a user (in the caller's session).

	:param action: one of USER_ACTIONS"""
	if action == "approve":
		user.is_approved = True
		user.is_active = True
		clear_lockout(user)
	elif action == "enable":
		# also after too many wrong passwords: the count starts again
		user.is_active = True
		clear_lockout(user)
	elif action == "disable":
		user.is_active = False
	elif action == "promote":
		user.role = Role.ADMIN
		user.is_approved = True
		user.is_active = True
		clear_lockout(user)
	elif action == "demote":
		user.role = Role.OPERATOR
	elif action == "delete":
		db_session.delete(user)
	elif action == "reset_2fa":
		# They enroll a new authenticator at their next sign-in (start_otp_flow
		# sends users without a secret to enrollment); the factory admin's 2FA
		# is turned off (it's optional there). LDAP users don't use 2FA.
		user.otp_secret = None
	elif action == "terminate_session":
		current_app.sessions.end_for(user.id)


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
	                       factory_ids={u.id for u in users if is_factory_admin(u)},
	                       active_section="users",
	                       session_user_ids=set(current_app.sessions.signed_in(time.time())))


@bp.route("/users/<uuid:user_id>/<action>", methods=["POST"])
@login_required
@require_admin
def admin_user_action(user_id: uuid.UUID, action: str) -> ResponseReturnValue:
	"""One action on one user (user_action_factory), audited as
	user.<action>. On the factory admin only FACTORY_ACTIONS (its own Reset
	2FA neither: My Account's turn-off asks for proof); an admin can't
	disable or delete their own account; none may take away the last
	administrator who can sign in (refused in words)."""
	if action not in USER_ACTIONS:
		return err("Unknown action", 404)
	if action in ("disable", "delete") and user_id == current_user.id:
		flash("You cannot perform this action on your own account.", "danger")
		return redirect(url_for("admin_users.admin_users"))
	try:
		with current_app.backend.postgres.get_session() as db_session:
			user = db_session.query(User).filter_by(id=user_id).first()

			if not user or (is_factory_admin(user) and action not in FACTORY_ACTIONS):
				return redirect(url_for("admin_users.admin_users"))
			if _own_factory_2fa(user, action):
				flash(OWN_2FA, "danger")
				return redirect(url_for("admin_users.admin_users"))
			if action in REMOVES_ADMIN:
				Accounts(db_session).check_admins_left({user.id})

			target_username = user.username
			target_id = user.id
			user_action_factory(user, action, db_session)
	except AccountError as e:
		flash(str(e), "danger")
		return redirect(url_for("admin_users.admin_users"))
	current_app.web.audit(AuditAction(f"user.{action}"), object_type="User",
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
		if is_factory_admin(user):
			return err("The factory admin's password can't be reset", 400)
		if user.auth_type != AuthType.LOCAL:
			return err("LDAP passwords are managed in the directory", 400)
		temporary = temporary_password(user.username)
		user.password_hash = generate_password_hash(temporary)
		user.must_change_password = True
		clear_lockout(user)
		username = user.username
	ended = current_app.sessions.end_for(user_id)
	current_app.web.audit(AuditAction.USER_RESET_PASSWORD, object_type="User",
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
	role = str(data.get("role", Role.OPERATOR))
	temporary = temporary_password(username)
	with current_app.backend.postgres.get_session() as db_session:
		try:
			user = Accounts(db_session).new_local(
				username=username, email=str(data.get("email", "")),
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
	current_app.web.audit(AuditAction.USER_CREATED, object_type="User", object_id=user_id,
	                      object_label=username, detail={"role": role})
	return ok(username=username, role=role, temporary_password=temporary)


@bp.route("/users/bulk/<action>", methods=["POST"])
@login_required
@require_admin
def admin_bulk_action(action: str) -> ResponseReturnValue:
	"""One action on the users ticked (form user_ids, comma separated) - the
	factory admin (but for FACTORY_ACTIONS, and its own Reset 2FA) and, for
	disable / delete, the admin's own account skipped; refused as a whole when it would take away
	the last administrator who can sign in. One audit row:
	user.bulk_<action> with the names."""
	if action not in USER_ACTIONS:
		return err("Unknown action", 404)
	raw = request.form.get("user_ids", "")
	try:
		user_ids = [uuid.UUID(uid.strip()) for uid in raw.split(",") if
		            uid.strip()]
	except ValueError:
		return redirect(url_for("admin_users.admin_users"))
	affected: list[str] = []
	try:
		with current_app.backend.postgres.get_session() as db_session:
			chosen: list[User] = []
			for uid in user_ids:
				user = db_session.get(User, uid)
				if not user or (is_factory_admin(user) and action not in FACTORY_ACTIONS):
					continue
				if _own_factory_2fa(user, action):
					continue
				if action in ("disable", "delete") and uid == current_user.id:
					continue
				chosen.append(user)
			if action in REMOVES_ADMIN:
				Accounts(db_session).check_admins_left(u.id for u in chosen)
			for user in chosen:
				affected.append(user.username)
				user_action_factory(user, action, db_session)
	except AccountError as e:
		flash(str(e), "danger")
		return redirect(url_for("admin_users.admin_users"))
	current_app.web.audit(AuditAction(f"user.bulk_{action}"),
	                      detail={"count": len(affected), "users": affected})
	return redirect(url_for("admin_users.admin_users"))


@bp.route("/sessions")
@login_required
@require_admin
def admin_sessions() -> str:
	"""Live Sessions: the users signed in now (a session within its limits),
	local and LDAP apart, with how long ago their newest sign-in was."""
	now = time.time()
	signed_in = current_app.sessions.signed_in(now)
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
	                                       s["auth_type"] == AuthType.LOCAL],
	                       ldap_sessions=[s for s in sessions if
	                                      s["auth_type"] == AuthType.LDAP],
	                       active_section="sessions")


@bp.route("/sessions/<uuid:user_id>/kick", methods=["POST"])
@login_required
@require_admin
def admin_sessions_kick(user_id: uuid.UUID) -> ResponseReturnValue:
	"""Sign the user out everywhere - every session of theirs, on every
	computer (as Terminate Session). An open page notices within 30 s.

	:returns: ok; 400 for the admin's own sessions (Sign out ends those);
	 404 when they have no session"""
	if user_id == current_user.id:
		return err("You can't end your own session here - use Sign out", 400)
	ended = current_app.sessions.end_for(user_id)
	if not ended:
		return err("Session not found", 404)
	current_app.web.audit(AuditAction.ADMIN_SESSION_KICK, object_type="User",
	                      object_id=user_id, success=True,
	                      detail={"sessions_ended": ended})
	return ok()
