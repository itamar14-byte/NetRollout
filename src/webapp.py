import base64
import secrets
import uuid
from io import BytesIO
from itertools import groupby
from queue import Empty
from time import sleep
from collections import Counter

import pyotp
import qrcode
from flask import (redirect, Response, request, render_template, url_for, \
                   Flask, flash, session)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (LoginManager, login_required,
                         logout_user, current_user, login_user)
from flask_wtf import CSRFProtect

from sqlalchemy.exc import IntegrityError
from waitress import serve
from werkzeug.security import generate_password_hash, check_password_hash

import encryption
import input_parser
from core import RolloutOptions
from db import get_session
from orchestration import RolloutOrchestrator
from tables import User, DeviceResult

app = Flask(__name__, template_folder='../templates')
app.config["SECRET_KEY"] = secrets.token_urlsafe(32)

orchestrator = RolloutOrchestrator()

login_mng = LoginManager()
login_mng.init_app(app)
login_mng.login_view = "home"

conn_limit = Limiter(get_remote_address, app=app, default_limits=[],
                     storage_uri="memory://")

csrf = CSRFProtect(app)


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
						return redirect(url_for("dashboard"))
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
				user.otp_secret = encryption.encrypt(otp_secret)
				db_session.flush()
				db_session.expunge(user)
			session.pop("pending_totp_secret")
			session.pop("pre_auth_user_id")
			login_user(user)
			return redirect(url_for("dashboard"))
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
		if pyotp.TOTP(encryption.decrypt(user.otp_secret)).verify(user_code, valid_window=1):
			login_user(user)
			session.pop("pre_auth_user_id")
			return redirect(url_for("dashboard"))
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
	with get_session() as db_session:
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

	# Options
	verbose_flag = request.form.get("_verbose", "")
	verify_flag = request.form.get("_verify", "")
	options = RolloutOptions(verify=bool(verify_flag),
	                         verbose=bool(verbose_flag),
	                         webapp=True)

	# Load inventory from db
	with get_session() as db_session:
		user = db_session.get(User, current_user.id)
		device_inventory = user.inventory
		db_session.expunge_all()

	# Parse and Submit
	devices = input_parser.InputParser.import_from_inventory(device_inventory)
	job_id = orchestrator.submit(devices, commands, options, current_user.id)
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
		return redirect(url_for("dashboard"))
	return redirect(url_for("admin_users"))


@app.route("/admin/users")
@login_required
def admin_users():
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	with get_session() as db_session:
		users = db_session.query(User).order_by(User.created_at).all()
		db_session.expunge_all()
	return render_template("admin_users.html", users=users,
	                       active_section="users")


@app.route("/admin/users/<user_id>/<action>", methods=["POST"])
@login_required
def admin_user_action(user_id, action):
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	if action in ("disable", "delete") and uuid.UUID(user_id) == current_user.id:
		flash("You cannot perform this action on your own account.", "danger")
		return redirect(url_for("admin_users"))
	with get_session() as db_session:
		try:
			user = db_session.query(User).filter_by(
				id=uuid.UUID(user_id)).first()
		except ValueError:
			return redirect(url_for("dashboard"))

		if not user or user.username == "admin":
			return redirect(url_for("admin_users"))

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
		elif action == "delete":
			db_session.delete(user)

	return redirect(url_for("admin_users"))


@app.route("/admin/users/bulk/<action>", methods=["POST"])
@login_required
def admin_bulk_action(action):
	if current_user.role != "admin":
		return redirect(url_for("dashboard"))
	raw = request.form.get("user_ids", "")
	try:
		user_ids = [uuid.UUID(uid.strip()) for uid in raw.split(",") if uid.strip()]
	except ValueError:
		return redirect(url_for("admin_users"))
	with get_session() as db_session:
		for uid in user_ids:
			user = db_session.get(User, uid)
			if not user or user.username == "admin":
				continue
			if action in ("disable", "delete") and uid == current_user.id:
				continue
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
			elif action == "delete":
				db_session.delete(user)
	return redirect(url_for("admin_users"))


def job_status(rows: list[DeviceResult]) -> str:
	statuses = {r.status for r in rows}
	if "cancelled" in statuses:
		return "cancelled"
	if all(r.status == "failed" for r in rows):
		return "failed"
	if any(r.status in ("failed","partial") for r in rows):
		return "partial"
	return "success"
@app.route("/dashboard")
@login_required
def dashboard():
	with get_session() as db_session:
		user = db_session.get(User, current_user.id)
		inventory_count = len(user.inventory)
		profile_count = len(user.security_profiles)
		jobs_results = user.results  # DeviceResult rows
		db_session.expunge_all()

	sorted_results = sorted(jobs_results, key=lambda x: x.job_id)
	job_summaries = []
	for job_id, rows in groupby(sorted_results, key=lambda x: x.job_id):
		rows = list(rows)
		job_summaries.append({"job_id": job_id,
		                      "completed_at": max(r.completed_at for r in
		                                          rows),
		                      "device_count": len(rows),
		                      "commands_sent": rows[0].commands_sent,
		                      "status": job_status(rows)})

	job_summaries.sort(key=lambda x: x['completed_at'], reverse=True)
	recent_jobs = job_summaries[:5]
	total_rollouts = len(job_summaries)
	last_status = job_summaries[0]['status'] if job_summaries else None

	#get active job
	job_id = session.get("job_id",None)
	active_job = orchestrator.get(uuid.UUID(job_id)) if job_id else None

	active_job_data = None
	if active_job and active_job.is_alive():
		active_job_data = {
			"job_id": job_id,
			"device_count": active_job.get_device_count(),
			"started_at": active_job.started_at.strftime("%H:%M:%S"),
			"started_at_iso": active_job.started_at.isoformat()
		}


	return render_template("dashboard.html",
	                       active_section="dashboard",
	                       active_job=active_job_data,
	                       recent_jobs=recent_jobs,
	                       inventory_count=inventory_count,
	                       profile_count=profile_count,
	                       total_rollouts=total_rollouts,
	                       last_status=last_status)


@app.route("/inventory")
@login_required
def inventory():
	return render_template("inventory.html",
	                       active_section="inventory")


@app.route("/results")
@login_required
def results():
	return render_template("results.html",
	                       active_section="results")


@app.route("/security")
@login_required
def security():
	return render_template("security.html",
	                       active_section="security")


if __name__ == "__main__":
	serve(app, host="0.0.0.0", port=8080)
