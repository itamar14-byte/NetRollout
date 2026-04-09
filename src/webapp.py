import base64
import secrets
import uuid
from io import BytesIO
from queue import Empty
from time import sleep

import pyotp
import qrcode
from flask import (redirect, Response, request, render_template, url_for, \
                   Flask, flash, session)
from flask_login import (LoginManager, login_required,
                         logout_user, current_user, login_user)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from sqlalchemy.exc import IntegrityError
from waitress import serve
from werkzeug.security import generate_password_hash, check_password_hash

import input_parser
from core import RolloutOptions
from orchestration import RolloutOrchestrator
from db import get_session
from logging_utils import RolloutLogger
from tables import User

app = Flask(__name__, template_folder='../templates')
app.config["SECRET_KEY"] = secrets.token_urlsafe(32)

orchestrator = RolloutOrchestrator()

login_mng = LoginManager()
login_mng.init_app(app)
login_mng.login_view = "home"

conn_limit = Limiter(get_remote_address, app=app, default_limits=[],
                     storage_uri="memory://")

@login_mng.user_loader
def load_user(user_id):
	with get_session() as db_session:
		try:
			user = db_session.get(User, uuid.UUID(user_id))
		except ValueError:
			return None
		if user:
			db_session.expunge(user)
		return user


@app.route("/")
def home():
	return render_template("index.html")


@app.route("/login", methods=["POST"])
@conn_limit.limit("10 per minute")
def login():
	username = request.form["username"]
	password = request.form["password"]
	with get_session() as db_session:
		user = db_session.query(User).filter_by(username=username).first()
		# checks credentials are correct
		if user and check_password_hash(user.password_hash, password):
			# checks user was activated
			if user.is_approved:
				if user.is_active:
					if user.username == "admin":
						db_session.expunge(user)
						login_user(user)
						return redirect(url_for("upload"))
					# checks otp enrollment
					elif user.otp_secret:
						session["pre_auth_user_id"] = str(user.id)
						return redirect(url_for("otp_verify"))
					else:
						flash("To complete enrollment,"
						      " you are referred to OPT set up portal"
						      , "info")
						session["pre_auth_user_id"] = str(user.id)
						return redirect(url_for("otp_enroll"))
				else:
					flash("User disabled, please check with administrator",
					      "danger")
					session.pop("pre_auth_user_id", None)
					return redirect(url_for("home"))
			else:
				flash("User still pending admin approval",
				      "danger")
				session.pop("pre_auth_user_id", None)
				return redirect(url_for("home"))
		else:
			flash("invalid credentials", "danger")
			session.pop("pre_auth_user_id", None)
			return redirect(url_for("home"))


@app.route("/register", methods=["GET"])
def register_form():
	return render_template("register.html")


@app.route("/register", methods=["POST"])
def register():
	username = request.form["username"]
	pass_hash = generate_password_hash(request.form["password"])
	email = request.form["email"]
	full_name = request.form["full_name"]
	position = request.form.get("position", None)

	new_user = User(username=username,
	                password_hash=pass_hash,
	                email=email,
	                full_name=full_name,
	                role="user",
	                position=position)

	with get_session() as db_session:
		try:
			db_session.add(new_user)
			db_session.flush()
			flash("Registration successful -"
			      " your account is pending admin "
			      "approval.", "success")
			return redirect(url_for("home"))
		except IntegrityError:
			flash("email or username already exists", "danger")
			return redirect(url_for("register_form"))


@app.route("/otp_enroll", methods=["GET", "POST"])
def otp_enroll():
	if request.method == "GET":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("home"))
		with get_session() as db_session:
			try:
				user = db_session.query(User).filter_by(
					id=uuid.UUID(user_id)).first()
			except ValueError:
				return redirect(url_for("home"))
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
			return redirect(url_for("home"))
		otp_secret = session.get("pending_totp_secret", None)
		user_code = request.form["code"]
		if pyotp.TOTP(otp_secret).verify(user_code, valid_window=1):
			with get_session() as db_session:
				try:
					user = db_session.query(User).filter_by(id=uuid.UUID(
						user_id)).first()
				except ValueError:
					return redirect(url_for("home"))
				user.otp_secret = otp_secret
				db_session.flush()
				db_session.expunge(user)
			session.pop("pending_totp_secret")
			session.pop("pre_auth_user_id")
			login_user(user)
			return redirect(url_for("upload"))
		flash("invalid code, please try again", "danger")
		return redirect(url_for("otp_enroll"))


@app.route("/otp_verify", methods=["GET", "POST"])
def otp_verify():
	if request.method == "POST":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("home"))
		user_code = request.form["code"]
		with get_session() as db_session:
			try:
				user = db_session.query(User).filter_by(
					id=uuid.UUID(user_id)).first()
			except ValueError:
				return redirect(url_for("home"))
			db_session.expunge(user)
		if not user.otp_secret:
			return redirect(url_for("otp_enroll"))
		if pyotp.TOTP(user.otp_secret).verify(user_code, valid_window=1):
			login_user(user)
			session.pop("pre_auth_user_id")
			return redirect(url_for("upload"))
		flash("invalid code, please try again", "danger")
		return redirect(url_for("otp_verify"))
	elif request.method == "GET":
		return render_template("otp_verify.html")


@app.route("/logout")
def logout():
	logout_user()
	return redirect(url_for("home"))


@app.route("/account")
@login_required
def account():
	return render_template("account.html", user=current_user)


@app.route("/upload")
@login_required
def upload():
	return render_template("upload.html")


@app.route("/start_rollout", methods=["POST"])
@login_required
def start_rollout():

	# File upload
	commands_file = request.files.get("commands_file")
	# Manual Entry
	manual_commands = request.form.get("manual_commands", "").strip()

	if commands_file:
		commands = [line.decode("utf-8").strip() for line in
	            commands_file.readlines()]
	else:
		commands = [line.strip() for line in manual_commands.splitlines() if
		            line.strip()]

	#Options
	verbose_flag = request.form.get("_verbose", "")
	verify_flag = request.form.get("_verify", "")
	options = RolloutOptions(verify=bool(verify_flag),
	                         verbose=bool(verbose_flag),
	                         webapp=True)


	#Load inventory from db
	with get_session() as db_session:
		user = db_session.get(User,current_user.id)
		inventory = user.inventory
		db_session.expunge_all()

	#Parse and Submit
	devices = input_parser.InputParser.import_from_inventory(inventory)
	job_id = orchestrator.submit(devices, commands, options)
	session["job_id"] = str(job_id)

	return redirect(url_for("rollout"))


@app.route("/rollout")
@login_required
def rollout():
	return render_template("rollout.html")


@app.route("/cancel_rollout", methods=["POST"])
@login_required
def cancel_rollout():
	job_id = session.get("job_id", None)
	if job_id:
		orchestrator.cancel(uuid.UUID(job_id))
		return {"status": "cancelled"}
	return {"status": "no_active_rollout"}



@app.route("/rollout_status")
@login_required
def get_rollout_status():
	job_id = session.get("job_id", None)
	if job_id:
		job = orchestrator.get(uuid.UUID(job_id))
		if job and job.is_alive():
			return {"status": "active"}
		return {"status": "idle"}
	return {"status": "idle"}


@app.route("/rollout_stream")
@login_required
def sse_stream():
	def generate():
		job_id = session.get("job_id", None)
		if not job_id:
			return
		job = orchestrator.get(uuid.UUID(job_id))
		if not job:
			return
		while job.is_alive():
			try:
				msg = job.get_log()
				yield f"data: {msg}\n\n"
			except Empty:
				yield "data: \n\n"
				sleep(0.5)

	return Response(
		generate(),
		mimetype="text/event-stream",
		headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
	)


@app.route("/admin")
@login_required
def admin_panel():
	if current_user.role != "admin":
		return redirect(url_for("upload"))
	return redirect(url_for("admin_users"))


@app.route("/admin/users")
@login_required
def admin_users():
	if current_user.role != "admin":
		return redirect(url_for("upload"))
	with get_session() as db_session:
		users = db_session.query(User).order_by(User.created_at).all()
		db_session.expunge_all()
	return render_template("admin_users.html", users=users,
	                       active_section="users")


@app.route("/admin/users/<user_id>/<action>", methods=["POST"])
@login_required
def admin_user_action(user_id, action):
	if current_user.role != "admin":
		return redirect(url_for("upload"))
	if action == "disable" and uuid.UUID(user_id) == current_user.id:
		flash("You cannot disable your own account.", "danger")
		return redirect(url_for("admin_users"))
	with get_session() as db_session:
		try:
			user = db_session.query(User).filter_by(
				id=uuid.UUID(user_id)).first()
		except ValueError:
			return redirect(url_for("upload"))


		if action == "approve":
			user.is_approved = True
			user.is_active = True
		elif action == "enable":
			user.is_active = True
		elif action == "disable":
			user.is_active = False
		elif action == "promote":
			user.role = "admin"
		elif action == "demote":
			user.role = "user"

	return redirect(url_for("admin_users"))


if __name__ == "__main__":
	serve(app, host="0.0.0.0", port=8080)
