"""Signing in and out: the sign-in page, local and LDAP accounts (an LDAP
user in a mapped group is created at the first sign-in), 2FA enrolment and
verification for local users, access requests, the password change, the
session's time left and the Account page."""
import base64
import uuid
from collections import Counter
from io import BytesIO
from typing import Any, cast
from urllib.parse import urlparse, urlsplit

import pyotp
import qrcode
from flask import Blueprint, Response, session, redirect, url_for, flash, render_template, request
from flask.typing import ResponseReturnValue
from flask_login import login_user, current_user, login_required, logout_user
from flask_session.base import ServerSideSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from werkzeug.security import check_password_hash, generate_password_hash

from src.accounts.ldap import Directory, LdapUnavailable
from src.accounts.users import RULE, password_problem, LIMITS, AccountError, Accounts
from src.audit import AuditAction
from src.db.tables import DeviceResult, LDAPServer, LDAPGroup, User, AuthType
from src.encryption import decrypt, encrypt
from src.jobs import job_status
from src.rollout.engine import DeviceStatus
from src.webapp.app import current_app
from src.webapp.hooks import (csrf, conn_limit, mark_signed_in, session_seconds_left,
                              signed_in_user)
from src.webapp.http import is_background, ok, with_form


bp = Blueprint("auth", __name__)


_LOGIN_FAIL_MESSAGES = {
	"invalid_credentials": "Invalid credentials",
	"account_disabled": "User disabled, please check with administrator",
	"pending_approval": "User still pending admin approval",
	"ldap_bind_failed": "Invalid credentials",
	"ldap_unavailable": "LDAP authentication service unavailable",
}


# Where to go after signing in. The sign-in page is opened with ?next=<path>
# (Flask-Login for app pages, nginx for /grafana/); it's kept in the session
# across the password, 2FA and forced-password-change steps and used once.
# Local paths only: a crafted ?next=https://evil... must not turn a real
# sign-in into a bounce to someone else's page.
NEXT_KEY = "login_next"

# Sign-in and both 2FA pages, per client address: a password or a 6-digit
# code can't be guessed at speed
SIGN_IN_RATE = "10 per minute"
# Wrong 2FA codes in a row before the half-done sign-in is dropped (the
# password must be given again - itself rate limited)
MAX_WRONG_CODES = 5
WRONG_CODES_KEY = "otp_wrong_codes"
# What a half-done sign-in keeps in the session until the code completes it
_PENDING_2FA = ("pre_auth_user_id", "pending_totp_secret", WRONG_CODES_KEY)


def safe_next(value: str | None) -> str | None:
	"""`value` if it's a path on this site worth returning to, else None."""
	if not value or not value.startswith("/") or value.startswith("//"):
		return None
	if "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
		return None
	parts = urlsplit(value)
	if parts.scheme or parts.netloc or parts.path in ("/", "/logout"):
		return None
	return value


def after_login(user: User) -> ResponseReturnValue:
	"""The redirect that ends a successful sign-in: the page asked for, else
	the Dashboard. A user who must change the password goes through the
	password gate first; the page waits for the change."""
	mark_signed_in()
	if user.must_change_password:
		return redirect(url_for("jobs.dashboard"))
	return redirect(session.pop(NEXT_KEY, None) or url_for("jobs.dashboard"))


def login_fail(username: str, reason: str,
               actor_id: uuid.UUID | None = None) -> ResponseReturnValue:
	"""Every failed sign-in ends here: the message for the person, a failed
	audit row with the reason (a key of _LOGIN_FAIL_MESSAGES), and any
	half-done 2FA state cleared so a stale pre_auth_user_id can't be replayed.

	:returns: the way back to the sign-in page"""
	flash(_LOGIN_FAIL_MESSAGES.get(reason, "Login failed"), "danger")
	current_app.web.audit(AuditAction.AUTH_LOGIN, success=False, username=username,
	           actor_id=actor_id, detail={"reason": reason})
	session.pop("pre_auth_user_id", None)
	return redirect(url_for("auth.home"))


def complete_login(user: User, db_session: Session,
                   **audit_detail: Any) -> ResponseReturnValue:
	"""Sign the user in (no second factor: the factory admin, LDAP users) and
	audit it. The user is detached first, so Flask-Login holds no live ORM
	object across requests; the session is indexed at once (Live Sessions).

	:param audit_detail: added to the audit row (e.g. auth_type)"""
	db_session.expunge(user)
	return _sign_in(user, **audit_detail)


def _sign_in(user: User, **audit_detail: Any) -> ResponseReturnValue:
	"""Every sign-in ends here, with or without 2FA: a new session id (one
	known before the sign-in - planted, or seen on a shared computer - is
	worthless after it; what the session held carries over), the user signed
	in, audited as auth.login.

	:param user: detached
	:param audit_detail: added to the audit row"""
	current_app.session_interface.regenerate(session)
	for key in _PENDING_2FA:
		session.pop(key, None)
	login_user(user)
	current_app.web.audit(AuditAction.AUTH_LOGIN, success=True, username=user.username,
	                      actor_id=user.id, detail=audit_detail or None)
	return after_login(user)


def start_otp_flow(user: User) -> ResponseReturnValue:
	"""The password was right: on to the second factor - verification, or
	enrolment for a user without an authenticator yet. The user id waits in
	the session (pre_auth_user_id) for otp_verify / otp_enroll. Audited as
	auth.password_ok: not a sign-in yet."""
	session["pre_auth_user_id"] = str(user.id)
	session.pop(WRONG_CODES_KEY, None)
	current_app.web.audit(AuditAction.AUTH_PASSWORD_OK, success=True, username=user.username,
	                      actor_id=user.id)
	if user.otp_secret:
		return redirect(url_for("auth.otp_verify"))
	flash("To complete enrollment, you are referred to OTP set up portal",
	      "info")
	return redirect(url_for("auth.otp_enroll"))

def login_local(user: User, password: str, db_session: Session) -> ResponseReturnValue:
	"""A local account's sign-in: the password, then approved, then active;
	then 2FA - except the factory admin, who must stay reachable for
	recovery."""
	assert user.password_hash is not None   # every local account has one
	if not check_password_hash(user.password_hash, password):
		return login_fail(user.username, "invalid_credentials", user.id)
	if not user.is_approved:
		return login_fail(user.username, "pending_approval", user.id)
	if not user.is_active:
		return login_fail(user.username, "account_disabled", user.id)
	if user.username == "admin":
		return complete_login(user, db_session)
	return start_otp_flow(user)


def login_ldap_existing(user: User, password: str,
                        db_session: Session) -> ResponseReturnValue:
	"""A known LDAP user's sign-in: their directory must still be configured
	and active; a bind with the password; then approved and active."""
	ldap_server = user.ldap_server
	if not ldap_server or not ldap_server.is_active:
		flash("LDAP auth service unavailable", "danger")
		return redirect(url_for("auth.home"))
	try:
		bound = Directory(ldap_server).user_bind(user.username, password)
	except LdapUnavailable:
		return login_fail(user.username, "ldap_unavailable", user.id)
	if not bound:
		return login_fail(user.username, "ldap_bind_failed", user.id)
	if not user.is_approved:
		return login_fail(user.username, "pending_approval", user.id)
	if not user.is_active:
		return login_fail(user.username, "account_disabled", user.id)
	return complete_login(user, db_session, auth_type=AuthType.LDAP)


def login_ldap_group(username: str, password: str,
                     db_session: Session) -> ResponseReturnValue:
	"""An unknown username: if the active directory accepts the password and
	the user is in one of its mapped groups, the account is created (approved,
	active, the group's role) and signed in; else invalid credentials."""
	ldap_server = db_session.query(LDAPServer).filter_by(is_active=True).first()
	if ldap_server:
		groups = db_session.query(LDAPGroup).filter_by(
			ldap_server_id=ldap_server.id, is_active=True).all()
		if groups:
			directory = Directory(ldap_server)
			# check_group_membership performs the bind and returns (group_dn, role)
			# if the user is a member of any mapped group, None otherwise.
			try:
				match = directory.check_group_membership(username, password, groups)
			except LdapUnavailable:
				return login_fail(username, "ldap_unavailable")
			if match:
				group_dn, role = match
				# Fetch display attributes from LDAP directory; tolerate failure.
				details = directory.user_details(username)
				# Auto-provisioned users are pre-approved and active; role comes
				# from the matched group mapping.
				user = Accounts(db_session).new_ldap(username, ldap_server.id, role, details)
				# Commit (not just flush) before complete_login: its audit row is
				# written in a separate session with actor_id -> this user, which
				# must already be visible there or the FK insert fails. Refresh
				# reloads the attributes the commit expired, so expunge works.
				db_session.commit()
				db_session.refresh(user)
				return complete_login(user, db_session,
				                      auth_type=AuthType.LDAP, auto_created=True,
				                      group_dn=group_dn)
	# No server, no matching group, or bind failed — treat as bad credentials.
	return login_fail(username, "invalid_credentials")


@bp.route("/")
def home() -> str:
	"""The sign-in page; ?next= (a path on this site) is kept for after it."""
	target = safe_next(request.args.get("next"))
	if target:
		session[NEXT_KEY] = target
	return render_template("index.html")

@bp.route("/login", methods=["GET"])
def login_get() -> ResponseReturnValue:
	"""The sign-in page (/login is where its form posts); a safe ?next= is
	kept, so a linked /login?next=<path> still lands there after signing in."""
	target = safe_next(request.args.get("next"))
	return redirect(url_for("auth.home", next=target) if target else url_for("auth.home"))

@bp.route("/login", methods=["POST"])
@csrf.exempt
@conn_limit.limit(SIGN_IN_RATE)
@with_form("username", "password")
def login(data: Any) -> ResponseReturnValue:
	"""Sign in (rate limited): a local account, a known LDAP user, or an LDAP
	user in a mapped group."""
	# Origin check replaces CSRF for login — blocks cross-origin POSTs without
	# depending on redis_session state, so it survives server restarts.
	# Compare hostnames only: scheme/port vary under reverse proxy.
	origin = request.headers.get("Origin") or request.headers.get("Referer", "")
	if origin:
		origin_host = urlparse(origin).hostname or ""
		server_host = request.host.split(":")[0]
		if origin_host and origin_host != server_host:
			return redirect(url_for("auth.home"))
	username = data["username"]
	password = data["password"]
	with current_app.backend.postgres.get_session() as db_session:
		accounts = Accounts(db_session)
		user = accounts.by_name(username)
		if user and user.auth_type == AuthType.LOCAL:
			return login_local(user, password, db_session)
		elif user and user.auth_type == AuthType.LDAP:
			return login_ldap_existing(user, password, db_session)
		# Directories match names whatever their case: "Alice" is the LDAP
		# account "alice" (its state and role apply - a new account from a
		# group would bypass a disable or a demotion). Another account under
		# other capitals is refused, never duplicated.
		same_name = accounts.by_name_ci(username)
		ldap_users = [u for u in same_name if u.auth_type == AuthType.LDAP]
		if len(ldap_users) == 1 and len(same_name) == 1:
			return login_ldap_existing(ldap_users[0], password, db_session)
		if same_name:
			return login_fail(username, "invalid_credentials")
		return login_ldap_group(username, password, db_session)

@bp.route("/register", methods=["GET"])
def register_form() -> str:
	return render_template("register.html")


@bp.route("/register", methods=["POST"])
@with_form("username", "password", "email", "full_name")
def register(data: Any) -> ResponseReturnValue:
	"""A person's access request: an operator account waiting for an admin's
	approval (src/accounts/users.py - the same checks as an admin's Add user)."""
	username = data["username"].strip()
	with current_app.backend.postgres.get_session() as db_session:
		try:
			Accounts(db_session).new_local(
				username=username, email=data["email"], full_name=data["full_name"],
				position=data.get("position"), password=data["password"])
		except AccountError as e:
			db_session.rollback()
			# what was typed may be longer than the audit's column (that can
			# be why it's refused)
			current_app.web.audit(AuditAction.AUTH_REGISTER, success=False,
			                      username=username[:LIMITS["username"]] or "anonymous",
			                      detail={"reason": str(e)})
			flash(str(e), "danger")
			return redirect(url_for("auth.register_form"))
		except IntegrityError:
			# two requests at once for the same name: the database refuses the
			# second. The failed flush leaves the session unusable - roll back.
			db_session.rollback()
			current_app.web.audit(AuditAction.AUTH_REGISTER, success=False, username=username,
			                      detail={"reason": "duplicate_username_or_email"})
			flash("That username or email address is already in use.", "danger")
			return redirect(url_for("auth.register_form"))
	current_app.web.audit(AuditAction.AUTH_REGISTER, success=True, username=username,
	                      object_type="User", object_label=username)
	flash("Registration successful - your account is pending admin approval.", "success")
	return redirect(url_for("auth.home"))

def _pending_user() -> User | None:
	"""The user whose password the half-done sign-in proved (detached); None
	without one - or when the account is gone."""
	user_id = session.get("pre_auth_user_id")
	if not user_id:
		return None
	with current_app.backend.postgres.get_session() as db_session:
		try:
			user = db_session.get(User, uuid.UUID(user_id))
		except ValueError:
			return None
		if user is not None:
			db_session.expunge(user)
		return user


def _wrong_code(user: User, retry_endpoint: str) -> ResponseReturnValue:
	"""A wrong 2FA code: audited (a failed auth.login, wrong_code); the
	MAX_WRONG_CODES-th in a row drops the half-done sign-in, so the password
	must be given again.

	:param retry_endpoint: where to try again while tries are left"""
	wrong = session.get(WRONG_CODES_KEY, 0) + 1
	if wrong >= MAX_WRONG_CODES:
		for key in _PENDING_2FA:
			session.pop(key, None)
		current_app.web.audit(AuditAction.AUTH_LOGIN, success=False, username=user.username,
		                      actor_id=user.id, detail={"reason": "too_many_wrong_codes"})
		flash("Too many wrong codes - sign in again.", "danger")
		return redirect(url_for("auth.home"))
	session[WRONG_CODES_KEY] = wrong
	current_app.web.audit(AuditAction.AUTH_LOGIN, success=False, username=user.username,
	                      actor_id=user.id, detail={"reason": "wrong_code"})
	flash("invalid code, please try again", "danger")
	return redirect(url_for(retry_endpoint))


@bp.route("/otp_enroll", methods=["GET", "POST"])
@conn_limit.limit(SIGN_IN_RATE, methods=["POST"])
@with_form("code")
def otp_enroll(data: Any) -> ResponseReturnValue:
	"""2FA enrolment after the password: GET shows a new authenticator's QR
	code (its secret waits in the session); POST checks a code from it, then
	stores the secret (encrypted) and signs in. Rate limited; wrong codes
	count (_wrong_code). A user who has 2FA already goes to the code page -
	only an admin's 2FA reset allows a new enrolment (else the password
	alone would replace the user's authenticator)."""
	user = _pending_user()
	if user is None:
		return redirect(url_for("auth.home"))
	if user.otp_secret:
		session.pop("pending_totp_secret", None)
		return redirect(url_for("auth.otp_verify"))
	if request.method == "GET":
		secret = session.get("pending_totp_secret") or pyotp.random_base32()
		session["pending_totp_secret"] = secret
		uri = pyotp.TOTP(secret).provisioning_uri(user.username,
		                                          issuer_name="NetRollout")
		img = qrcode.make(uri)
		buffer = BytesIO()
		img.save(buffer, format="png")
		buffer.seek(0)
		qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
		return render_template("otp_enroll.html", qr=qr_b64)

	otp_secret = session.get("pending_totp_secret")
	if not otp_secret:                      # the QR page wasn't shown first
		return redirect(url_for("auth.otp_enroll"))
	if not pyotp.TOTP(otp_secret).verify(data["code"], valid_window=1):
		return _wrong_code(user, "auth.otp_enroll")
	with current_app.backend.postgres.get_session() as db_session:
		stored = db_session.get(User, user.id)
		if stored is None:
			return redirect(url_for("auth.home"))
		stored.otp_secret = encrypt(otp_secret)
	return _sign_in(user)


@bp.route("/otp_verify", methods=["GET", "POST"])
@conn_limit.limit(SIGN_IN_RATE, methods=["POST"])
@with_form("code")
def otp_verify(data: Any) -> ResponseReturnValue:
	"""2FA after the password: POST checks the code and signs in. Rate
	limited; wrong codes count (_wrong_code)."""
	if request.method == "GET":
		return render_template("otp_verify.html")
	user = _pending_user()
	if user is None:
		return redirect(url_for("auth.home"))
	if not user.otp_secret:
		return redirect(url_for("auth.otp_enroll"))
	if not pyotp.TOTP(decrypt(user.otp_secret)).verify(data["code"], valid_window=1):
		return _wrong_code(user, "auth.otp_verify")
	return _sign_in(user)


@bp.route("/logout")
def logout() -> ResponseReturnValue:
	# Anonymous requests (stale tab, double click) just land on the login page
	if current_user.is_authenticated:
		current_app.web.audit(AuditAction.AUTH_LOGOUT)
	logout_user()
	session.clear()
	return redirect(url_for("auth.home"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
@conn_limit.limit("10 per minute", methods=["POST"])
def change_password() -> ResponseReturnValue:
	"""Pick a new password: forced (must_change_password — the seeded admin,
	or after an admin reset; the gate in extensions.py sends every page
	here) or voluntary, from the Account page. Local accounts only."""
	if current_user.auth_type != AuthType.LOCAL:
		flash("Your password is managed by the directory (LDAP) — change it "
		      "there.", "info")
		return redirect(url_for("auth.account"))
	forced = current_user.must_change_password
	if request.method == "GET":
		return render_template("change_password.html", forced=forced,
		                       rule=RULE)

	current = request.form.get("current_password", "")
	new = request.form.get("new_password", "")
	with current_app.backend.postgres.get_session() as db_session:
		user = signed_in_user(db_session)
		# A forced change follows the sign-in that just proved the password
		# (the factory admin's, or an admin reset's temporary one): asking for
		# it again adds nothing. A voluntary change proves it here.
		stored = user.password_hash
		assert stored is not None   # a local account (checked above) has one
		problem: str | None
		if not forced and not check_password_hash(stored, current):
			reason, problem = "wrong_current", "The current password is incorrect."
		elif new != request.form.get("confirm_password", ""):
			reason, problem = "mismatch", "The new passwords don't match."
		elif forced and check_password_hash(stored, new):
			reason, problem = "rule", "The new password must differ from the current one."
		else:
			reason, problem = "rule", password_problem(new, user.username,
			                                             None if forced else current)
		if problem:
			current_app.web.audit(AuditAction.AUTH_PASSWORD_CHANGE, success=False,
			                      detail={"reason": reason, "forced": forced})
			flash(problem, "danger")
			return redirect(url_for("auth.change_password"))
		user.password_hash = generate_password_hash(new)
		user.must_change_password = False

	# A new session id: one captured before the change is worthless after it.
	# Every other session of this user ends (e.g. a thief's, the reason for
	# the change); this one stays signed in.
	current_app.session_interface.regenerate(session)
	ended = current_app.sessions.end_for(current_user.id,
	                                     cast(ServerSideSession, session).sid)
	current_app.web.audit(AuditAction.AUTH_PASSWORD_CHANGE,
	                      detail={"forced": forced, "other_sessions_ended": ended})
	flash("Password changed.", "success")
	# a page asked for before a forced change waited for it
	return redirect(session.pop(NEXT_KEY, None) or url_for("jobs.dashboard"))


@bp.route("/account/session")
@login_required
def session_state() -> Response:
	"""How long this session has left — for the page's warning. Asked with
	X-NR-Background it doesn't count as activity; without ("Stay signed in")
	it does (the session gate extended it before this runs)."""
	idle_left, absolute_left = session_seconds_left()
	return ok(idle_seconds_left=int(idle_left),
	          absolute_seconds_left=int(absolute_left),
	          background=is_background())


@bp.route("/account")
@login_required
def account() -> str:
	"""The Account page: the user's details and their rollouts in numbers."""
	with current_app.backend.postgres.get_session() as db_session:
		user = signed_in_user(db_session)
		user_results = user.results
		db_session.expunge_all()

	by_job: dict[uuid.UUID, list[DeviceResult]] = {}
	for r in user_results:
		by_job.setdefault(r.job_id, []).append(r)
	total_rollouts = len(by_job)

	total_devices = len(user_results)

	if total_rollouts > 0:
		# a job's status as Results shows it - one failed device spoils it
		successful = sum(1 for rows in by_job.values() if job_status(rows) == DeviceStatus.SUCCESS)
		success_rate = round((successful / total_rollouts) * 100)
	else:
		success_rate = None

	if user_results:
		most_common_platform = \
			Counter(r.device_type for r in user_results).most_common(1)[0][0]
	else:
		most_common_platform = None

	total_commands = sum(r.commands_sent for r in user_results)

	return render_template("account.html",
	                       user=current_user,
	                       total_rollouts=total_rollouts,
	                       total_devices=total_devices,
	                       success_rate=success_rate,
	                       most_common_platform=most_common_platform,
	                       total_commands=total_commands)
