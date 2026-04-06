import uuid
import pyotp
import qrcode
import base64
from csv import DictReader
from io import TextIOWrapper, BytesIO
from json import loads
from queue import Empty
from threading import Event, Thread
from time import sleep

from flask import (redirect, Response, request, render_template, url_for, \
                   Flask, flash, session)
from flask_login import (LoginManager, login_required,
                         logout_user, current_user, login_user)
from sqlalchemy.exc import IntegrityError
from waitress import serve
from werkzeug.security import generate_password_hash, check_password_hash

from core import prepare_devices, RolloutEngine, RolloutOptions
from db import get_session
from logging_utils import LOG_QUEUE, base_notify
from tables import User

app = Flask(__name__, template_folder='../templates')
app.config["SECRET_KEY"] = "dev"
app.config["CURRENT_THREAD"] = None
cancel_event = Event()

login_mng = LoginManager()
login_mng.init_app(app)
login_mng.login_view = "home"


@login_mng.user_loader
def load_user(user_id):
	with get_session() as db_session:
		user = db_session.get(User, uuid.UUID(user_id))
		if user:
			db_session.expunge(user)
		return user


def webapp_input(
		device_file: BytesIO,
		commands_file: BytesIO,
		devices_json: str,
		manual_commands: str,
		verbose_flag: str,
		verify_flag: str,
) -> RolloutEngine:
	# Process Webapp input
	reader = (
		DictReader(TextIOWrapper(device_file, encoding="utf-8-sig"))
		if device_file
		else None
	)
	manual_devices = loads(devices_json) if devices_json else []

	txt_commands = (
		[line.decode("utf-8").strip() for line in commands_file.readlines()]
		if commands_file
		else []
	)
	manual_commands = [
		line.strip() for line in manual_commands.splitlines() if line.strip()
	]

	# Check which command option contains content and was chosen by the user,
	# and assigns the list of lines to the variable
	if txt_commands:
		commands = txt_commands
	else:
		commands = manual_commands

	verbose_bool = True if verbose_flag else False
	verify_bool = True if verify_flag else False

	required_keys = {
		"ip",
		"username",
		"password",
		"device_type",
		"secret",
		"port",
	}

	# Check if all required fields are there
	if device_file:
		missing_keys = required_keys - set(reader.fieldnames)
		if missing_keys:
			raise ValueError("Missing keys: {}".format(missing_keys))
	else:
		reader = None

	# process all validated devices from both sources into a list of dictionaries
	csv_devices = list(reader) if reader else []
	raw_devices = csv_devices + manual_devices
	devices = prepare_devices(raw_devices=raw_devices
	                          , verbose=verbose_bool,
	                          webapp=True,
	                          cancel_event=cancel_event)
	params = RolloutOptions(verbose=verbose_bool,
	                        verify=verify_bool,
	                        webapp=True)

	rollout_engine = RolloutEngine(param=params,
	                               devices=devices,
	                               commands=commands,
	                               cancel_event=cancel_event)

	# logs summary of file processing workflow
	base_notify(f"Devices loaded: {devices}", webapp=True, verbose=False)

	base_notify(
		f"Devices file successfully processed\n"
		f" {len(devices)} devices found\n"
		f"{len(commands)} commands will be executed",
		"green",
		webapp=True,
	)
	# return the processed data
	return rollout_engine


def background_rollout(
		device_file_stream,
		commands_file_stream,
		devices_json,
		manual_commands,
		verbose_flag,
		verify_flag, ):
	try:
		device_file = BytesIO(
			device_file_stream) if device_file_stream else None
		commands_file = (
			BytesIO(commands_file_stream) if commands_file_stream else None
		)

		run_engine = webapp_input(
			device_file,
			commands_file,
			devices_json,
			manual_commands,
			verbose_flag,
			verify_flag,
		)
		if run_engine.devices and run_engine.commands:
			run_engine.run()
			return None
		return None
	except Exception as e:
		base_notify(f"Rollout failed: {e}", "red", webapp=True)


@app.route("/")
def home():
	return render_template("index.html")


@app.route("/login", methods=["POST"])
def login():
	username = request.form["username"]
	password = request.form["password"]
	with get_session() as db_session:
		user = db_session.query(User).filter_by(username=username).first()
		# checks credentials are correct
		if user and check_password_hash(user.password_hash, password):
			#checks user was activated
			if user.is_active:
				if user.username == "admin":
					login_user(user)
					return redirect(url_for("upload"))
				#checks otp enrollment
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
				flash("User still pending admin approval",
				      "danger")
				return redirect(url_for("home"))

		flash("invalid credentials", "danger")
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
	role = request.form["role"]
	position = request.form.get("position", None)

	new_user = User(username=username,
	                password_hash=pass_hash,
	                email=email,
	                full_name=full_name,
	                role=role,
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

@app.route("/otp_enroll", methods=["GET","POST"])
def otp_enroll():
	if request.method == "GET":
		user_id = session.get("pre_auth_user_id",None)
		if not user_id:
			return redirect(url_for("home"))
		with get_session() as db_session:
			user = db_session.query(User).filter_by(id=user_id).first()
			db_session.expunge(user)
		totp = pyotp.random_base32()
		secret = session.get("pending_totp_secret",None) or totp
		session["pending_totp_secret"] = secret
		uri = pyotp.TOTP(session["pending_totp_secret"]).provisioning_uri(
			user.username,issuer_name="NetRollout")
		img = qrcode.make(uri)
		buffer = BytesIO()
		img.save(buffer,format="png")
		buffer.seek(0)
		qr_b64 = base64.b64encode(buffer.getvalue()).decode("utf8")
		return render_template("otp_enroll.html",qr=qr_b64)

	if request.method == "POST":
		user_id = session.get("pre_auth_user_id",None)
		if not user_id:
			return redirect(url_for("home"))
		otp_secret = session.get("pending_totp_secret",None)
		user_code = request.form["code"]
		if pyotp.TOTP(otp_secret).verify(user_code, valid_window=2):
			with get_session() as db_session:
				user = db_session.query(User).filter_by(id=user_id).first()
				user.otp_secret = otp_secret
				db_session.expunge(user)
			session.pop("pending_totp_secret")
			session.pop("pre_auth_user_id")
			login_user(user)
			return redirect(url_for("upload"))
		flash("invalid code, please try again", "danger")
		return redirect(url_for("otp_enroll"))

@app.route("/otp_verify", methods=["GET","POST"])
def otp_verify():
	if request.method == "POST":
		user_id = session.get("pre_auth_user_id", None)
		if not user_id:
			return redirect(url_for("home"))
		user_code = request.form["code"]
		with get_session() as db_session:
			user = db_session.query(User).filter_by(id=user_id).first()
			db_session.expunge(user)
		if pyotp.TOTP(user.otp_secret).verify(user_code, valid_window=2):
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
	cancel_event.clear()

	# File uploads
	device_file = request.files.get("device_file")
	commands_file = request.files.get("commands_file")
	device_file_stream = device_file.read() if device_file else None
	commands_file_stream = commands_file.read() if commands_file else None

	# Manual Entry
	devices_json = request.form.get("devices_json", "[]")
	if not devices_json:
		devices_json = "[]"
	manual_commands = request.form.get("manual_commands", "").strip()

	# General options
	verbose_flag = request.form.get("verbose", "")
	verify_flag = request.form.get("verify", "")

	# Runs the configuration push as a background task
	thread = Thread(
		target=lambda: background_rollout(
			device_file_stream,
			commands_file_stream,
			devices_json,
			manual_commands,
			verbose_flag,
			verify_flag,
		),
		daemon=True,
	)
	thread.start()
	app.config["CURRENT_THREAD"] = thread

	return redirect(url_for("rollout"))


@app.route("/rollout")
@login_required
def rollout():
	return render_template("rollout.html")


@app.route("/cancel_rollout", methods=["POST"])
@login_required
def cancel_rollout():
	if app.config["CURRENT_THREAD"] and app.config["CURRENT_THREAD"].is_alive():
		cancel_event.set()
		return {"status": "canceled"}
	else:
		return {"status": "no_active_rollout"}


@app.route("/rollout_status")
@login_required
def get_rollout_status():
	thread = app.config["CURRENT_THREAD"]
	if thread and thread.is_alive() and not cancel_event.is_set():
		return {"status": "active"}
	else:
		return {"status": "idle"}


@app.route("/rollout_stream")
@login_required
def sse_stream():
	def generate():
		while True:
			if cancel_event.is_set():
				yield "data: Rollout Canceled By User\n\n"
				break
			try:
				msg = LOG_QUEUE.get(timeout=1)
				yield f"data: {msg}\n\n"
			except Empty:
				yield "data: \n\n"
				sleep(0.5)

	return Response(
		generate(),
		mimetype="text/event-stream",
		headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
	)


if __name__ == "__main__":
	serve(app, host="0.0.0.0", port=8080)
