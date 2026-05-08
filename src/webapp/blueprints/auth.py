# python utilities
import base64
import uuid
from collections import Counter
from io import BytesIO
from urllib.parse import urlparse
import pyotp
import qrcode

#sevices
#flask
from flask import Blueprint, session, redirect, url_for, flash, current_app, \
	render_template, request
from flask_login import login_user, current_user, login_required, logout_user
#sqlalchemy
from sqlalchemy.exc import IntegrityError
#werkzug
from werkzeug.security import check_password_hash, generate_password_hash

#local modules
from src.db.tables import LDAPServer, LDAPGroup, User
from src.encryption import decrypt, encrypt
from src.ldap_auth import check_group_membership, fetch_user_details, user_bind
from src.webapp.extensions import csrf, conn_limit
from src.webapp.utils import with_form

bp = Blueprint("auth", __name__)

#######################Constants###############################
_LOGIN_FAIL_MESSAGES = {
	"invalid_credentials": "Invalid credentials",
	"account_disabled": "User disabled, please check with administrator",
	"pending_approval": "User still pending admin approval",
	"ldap_bind_failed": "Invalid credentials",
}


#######################Auth helpers###############################

def login_fail(username, reason, actor_id=None):
	# Single exit point for all failed auth paths — flashes the user-facing
	# message, writes a failed audit event with the machine reason, then clears
	# any partial OTP state so a stale pre_auth_user_id can't be replayed.
	flash(_LOGIN_FAIL_MESSAGES.get(reason, "Login failed"), "danger")
	current_app.web.audit("auth.login", success=False, username=username,
	           actor_id=actor_id, detail={"reason": reason})
	session.pop("pre_auth_user_id", None)
	return redirect(url_for("auth.home"))


def complete_login(user, db_session, **audit_detail):
	# Expunge before login_user so Flask-Login doesn't hold a live ORM object
	# across requests.
	# Redis session is registered immediately so the token is
	# valid on the very next ldap_request.
	db_session.expunge(user)
	login_user(user)
	record_redis_session(user.id)
	current_app.web.audit("auth.login", success=True, username=user.username,
	           actor_id=user.id, detail=audit_detail or None)
	return redirect(url_for("jobs.dashboard"))


def start_otp_flow(user):
	# Partial-auth checkpoint: store the user id, so otp_verify can finish the
	# login after the second factor is confirmed.
	session["pre_auth_user_id"] = str(user.id)
	current_app.web.audit("auth.login", success=True, username=user.username,
	           actor_id=user.id)
	# otp_secret present → user already enrolled, go straight to verify.
	# No secret → first-time setup, redirect to enroll portal instead.
	if user.otp_secret:
		return redirect(url_for("auth.otp_verify"))
	flash("To complete enrollment, you are referred to OTP set up portal",
	      "info")
	return redirect(url_for("auth.otp_enroll"))

def record_redis_session(user_id):
	sid = getattr(session, "sid", None)
	if sid is None:
		return
	current_app.backend.redis.client.set(f"user_session:{user_id}",
	                              sid, ex=86400)

def login_local(user, password, db_session):
	# Checks credentials are correct — bcrypt comparison against stored hash.
	if not check_password_hash(user.password_hash, password):
		return login_fail(user.username, "invalid_credentials", user.id)
	# Checks user was activated — approval first (admin decision), then active
	# flag (can be toggled independently after approval).
	if not user.is_approved:
		return login_fail(user.username, "pending_approval", user.id)
	if not user.is_active:
		return login_fail(user.username, "account_disabled", user.id)
	# admin bypasses OTP — built-in account has no OTP record and must always
	# be reachable for recovery even if the OTP service is down.
	if user.username == "admin":
		return complete_login(user, db_session)
	return start_otp_flow(user)


def login_ldap_existing(user, password, db_session):
	# User record exists with auth_type="ldap" — verify the bound LDAP server
	# is still reachable before attempting a bind.
	ldap_server = user.ldap_server
	if not ldap_server or not ldap_server.is_active:
		flash("LDAP auth service unavailable", "danger")
		return redirect(url_for("auth.home"))
	# Checks credentials are correct — bind attempt against the LDAP server
	# with the supplied password.
	if not user_bind(ldap_server, user.username, password):
		return login_fail(user.username, "ldap_bind_failed", user.id)
	# Checks user was activated — the same approval/active gate as the local
	# path.
	if not user.is_approved:
		return login_fail(user.username, "pending_approval", user.id)
	if not user.is_active:
		return login_fail(user.username, "account_disabled", user.id)
	return complete_login(user, db_session, auth_type="ldap")


def login_ldap_group(username, password, db_session):
	# Unknown username — no local record.
	# Try LDAP group-based auto-provisioning:
	# if an active LDAP server is configured and the user belongs to one of its
	# mapped groups, create a local record on the fly and complete the login.
	ldap_server = db_session.query(LDAPServer).filter_by(is_active=True).first()
	if ldap_server:
		groups = db_session.query(LDAPGroup).filter_by(
			ldap_server_id=ldap_server.id, is_active=True).all()
		if groups:
			# check_group_membership performs the bind and returns (group_dn, role)
			# if the user is a member of any mapped group, None otherwise.
			match = check_group_membership(ldap_server, username, password,
			                               groups)
			if match:
				group_dn, role = match
				# Fetch display attributes from LDAP directory; tolerate failure.
				details = fetch_user_details(ldap_server, username)
				email = details.get("email") if details else None
				full_name = details.get("full_name") if details else None
				# Auto-provisioned users are pre-approved and active; role comes
				# from the matched group mapping.
				user = User(username=username, auth_type="ldap",
				            ldap_server_id=ldap_server.id, role=role,
				            is_approved=True, is_active=True,
				            password_hash=None, email=email,
				            full_name=full_name)
				db_session.add(user)
				# flush to get user.id before the session commits so complete_login
				# can expunge the object.
				db_session.flush()
				return complete_login(user, db_session,
				                      auth_type="ldap", auto_created=True,
				                      group_dn=group_dn)
	# No server, no matching group, or bind failed — treat as bad credentials.
	return login_fail(username, "invalid_credentials")



#######################Routes###############################
@bp.route("/")
def home():
	return render_template("index.html")

@bp.route("/login", methods=["GET"])
def login_get():
	return redirect(url_for("auth.home"))

@bp.route("/login", methods=["POST"])
@csrf.exempt
@conn_limit.limit("10 per minute")
@with_form("username", "password")
def login(data):
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
		user = db_session.query(User).filter_by(username=username).first()
		if user and user.auth_type == "local":
			return login_local(user, password, db_session)
		elif user and user.auth_type == "ldap":
			return login_ldap_existing(user, password, db_session)
		else:
			return login_ldap_group(username, password, db_session)

@bp.route("/register", methods=["GET"])
def register_form():
	return render_template("register.html")


@bp.route("/register", methods=["POST"])
@with_form("username", "password", "email", "full_name")
def register(data):
	username = data["username"]
	pass_hash = generate_password_hash(data["password"])
	email = data["email"]
	full_name = data["full_name"]
	position = data.get("position", None)

	new_user = User(username=username,
	                password_hash=pass_hash,
	                email=email,
	                full_name=full_name,
	                role="user",
	                position=position)

	with current_app.backend.postgres.get_session() as db_session:
		try:
			db_session.add(new_user)
			db_session.flush()
			current_app.web.audit("auth.register", success=True,
			                    username=username,
			      object_type="User", object_label=username)
			flash("Registration successful -"
			      " your account is pending admin "
			      "approval.", "success")
			return redirect(url_for("auth.home"))
		except IntegrityError:
			current_app.web.audit("auth.register", success=False,
			                   username=username,
			      detail={"reason": "duplicate_username_or_email"})
			flash("email or username already exists", "danger")
			return redirect(url_for("auth.register_form"))


@bp.route("/otp_enroll", methods=["GET", "POST"])
@with_form("code")
def otp_enroll(data):
	if request.method == "GET":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("auth.home"))
		with current_app.backend.postgres.get_session() as db_session:
			try:
				user = db_session.query(User).filter_by(
					id=uuid.UUID(user_id)).first()
			except ValueError:
				return redirect(url_for("auth.home"))
			if not user:
				return redirect(url_for("auth.home"))
			db_session.expunge(user)
		totp = pyotp.random_base32()
		secret = session.get("pending_totp_secret", None) or totp
		session["pending_totp_secret"] = secret
		uri = pyotp.TOTP(session["pending_totp_secret"]).provisioning_uri(
			user.username, issuer_name="NetRollout")
		img = qrcode.make(uri)
		buffer = BytesIO()
		img.save(buffer, format="png")
		buffer.seek(0)
		qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
		return render_template("otp_enroll.html", qr=qr_b64)

	if request.method == "POST":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("auth.home"))
		otp_secret = session.get("pending_totp_secret", None)
		user_code = data["code"]
		if pyotp.TOTP(otp_secret).verify(user_code, valid_window=1):
			with current_app.backend.postgres.get_session() as db_session:
				try:
					user = db_session.query(User).filter_by(id=uuid.UUID(
						user_id)).first()
				except ValueError:
					return redirect(url_for("auth.home"))
				if not user:
					return redirect(url_for("auth.home"))
				user.otp_secret = encrypt(otp_secret)
				db_session.flush()
				db_session.expunge(user)
			session.pop("pending_totp_secret")
			session.pop("pre_auth_user_id")
			login_user(user)
			record_redis_session(user.id)
			return redirect(url_for("jobs.dashboard"))
		flash("invalid code, please try again", "danger")
		return redirect(url_for("auth.otp_enroll"))


@bp.route("/otp_verify", methods=["GET", "POST"])
@with_form("code")
def otp_verify(data):
	if request.method == "POST":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("auth.home"))
		user_code = data["code"]
		with current_app.backend.postgres.get_session() as db_session:
			try:
				user = db_session.query(User).filter_by(
					id=uuid.UUID(user_id)).first()
			except ValueError:
				return redirect(url_for("auth.home"))
			if not user:
				return redirect(url_for("auth.home"))
			db_session.expunge(user)
		if not user.otp_secret:
			return redirect(url_for("auth.otp_enroll"))
		if pyotp.TOTP(decrypt(user.otp_secret)).verify(user_code,
		                                                          valid_window=1):
			login_user(user)
			record_redis_session(user.id)
			session.pop("pre_auth_user_id")
			return redirect(url_for("jobs.dashboard"))
		flash("invalid code, please try again", "danger")
		return redirect(url_for("auth.otp_verify"))
	elif request.method == "GET":
		return render_template("otp_verify.html")


@bp.route("/logout")
def logout():
	current_app.web.audit("auth.logout")
	current_app.backend.redis.client.delete(f"user_session:{current_user.id}")
	logout_user()
	session.clear()
	return redirect(url_for("auth.home"))


@bp.route("/account")
@login_required
def account():
	with current_app.backend.postgres.get_session() as db_session:
		user = db_session.get(User, current_user.id)
		user_results = user.results
		db_session.expunge_all()

	# Total rollouts
	total_rollouts = len(set(r.job_id for r in user_results))

	# Total devices configured
	total_devices = len(user_results)

	# Success rate
	if total_rollouts > 0:
		successful = len(
			set(r.job_id for r in user_results if r.status == 'success'))
		success_rate = round((successful / total_rollouts) * 100)
	else:
		success_rate = None

	# Most configured device type
	if user_results:
		most_common_platform = \
			Counter(r.device_type for r in user_results).most_common(1)[0][0]
	else:
		most_common_platform = None

	# Total commands pushed
	total_commands = sum(r.commands_sent for r in user_results)

	return render_template("account.html",
	                       user=current_user,
	                       total_rollouts=total_rollouts,
	                       total_devices=total_devices,
	                       success_rate=success_rate,
	                       most_common_platform=most_common_platform,
	                       total_commands=total_commands)
