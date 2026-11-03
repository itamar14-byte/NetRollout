"""Security profiles: the credentials devices are reached with (stored
encrypted: SecurityProfiles in src/inventory.py) - create, edit, delete, and
test one against a device."""
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask.typing import ResponseReturnValue
from flask_login import login_required
from netmiko import NetmikoAuthenticationException, NetmikoTimeoutException

from src.audit import AuditAction
from src.db.tables import DeviceCredential, Inventory, SecurityProfile
from src.encryption import decrypt
from src.inventory import InventoryView, SecurityProfiles
from src.rollout import inputs
from src.rollout.address import is_ip, is_loopback, qualify, resolve
from src.rollout.detect import NEIGHBOUR_NAMES, detect
from src.rollout.engine import Device
from src.rollout.session import NetmikoSession
from src.webapp.app import current_app
from src.webapp.hooks import signed_in_user
from src.webapp.http import ok, err, own_server, with_json, with_form, flash_redirect, viewer

bp = Blueprint('security', __name__, url_prefix='/security')


def create_profile(label: str | None, username: str, password: str,
                   enable_secret: str | None) -> str:
	"""Save a new profile of the signed-in user's (SecurityProfiles.create)
	and audit it.

	:param label: its name; None: shown by its username
	:returns: its id"""
	with current_app.backend.postgres.get_session() as db_session:
		profile = SecurityProfiles(db_session, viewer()).create(label, username, password,
		                                                        enable_secret)
		db_session.flush()
		profile_id = str(profile.id)
	current_app.web.audit(AuditAction.SECURITY_PROFILE_CREATE, object_type="SecurityProfile",
	                      object_label=label or username)
	return profile_id


@bp.route("")
@login_required
def security() -> str:
	"""The profiles page: the user's profiles, each with the devices it's
	linked to (by this user - their own and global ones), and the devices
	they see, to link a profile to or test one against."""
	with current_app.backend.postgres.get_session() as db_session:
		user = signed_in_user(db_session)
		profiles = user.security_profiles
		view = InventoryView(db_session, viewer())
		devices = view.visible()
		mine = view.profiles(devices)
		db_session.expunge_all()
	linked: dict[str, list[Inventory]] = {str(p.id): [] for p in profiles}
	for device in devices:
		profile = mine[device.id]
		if profile is not None:
			linked[str(profile.id)].append(device)

	return render_template("security.html",
	                       profiles=profiles,
	                       devices=devices,
	                       linked=linked,
	                       my_profile_ids={str(d.id): str(p.id) if p else ""
	                                       for d in devices for p in [mine[d.id]]},
	                       active_section="security")


@bp.route("/create", methods=["POST"])
@login_required
@with_form("username", "password")
def security_create(data: Any) -> ResponseReturnValue:
	"""A new profile from the page's form (username and password required)."""
	label = data.get("label", "").strip() or None
	username = data.get("username", "").strip()
	password = data.get("password", "").strip()
	enable_secret = data.get("enable_secret", "").strip() or None

	create_profile(label, username, password, enable_secret)
	flash("Security profile created.", "success")
	return redirect(url_for("security.security"))


@bp.route("/quick_create", methods=["POST"])
@login_required
@with_json()
def security_quick_create(data: dict[str, Any]) -> ResponseReturnValue:
	"""A new profile from a modal elsewhere (inventory): JSON.

	:returns: {"status": "ok", id, label} or 422"""
	label = str(data.get("label", "") or "").strip() or None
	username = str(data.get("username", "") or "").strip()
	password = str(data.get("password", "") or "")
	enable_secret = str(data.get("enable_secret", "") or "").strip() or None
	if not username or not password:
		return err("Username and password are required", 422)

	profile_id = create_profile(label, username, password, enable_secret)
	return ok(id=profile_id, label=label or username)


@bp.route("/<uuid:profile_id>/edit", methods=["POST"])
@login_required
def security_edit(profile_id: uuid.UUID) -> ResponseReturnValue:
	"""Change a profile: an empty password or secret keeps the stored one;
	clear_enable_secret removes the secret (SecurityProfiles.update)."""
	with current_app.backend.postgres.get_session() as db_session:
		profiles = SecurityProfiles(db_session, viewer())
		profile = profiles.owned(profile_id)
		if profile is None:
			return redirect(url_for("security.security"))
		profiles.update(profile,
		                label=request.form.get("label", "").strip() or None,
		                username=request.form["username"],
		                password=request.form.get("password", "").strip(),
		                enable_secret=request.form.get("enable_secret", "").strip(),
		                clear_enable_secret=bool(request.form.get("clear_enable_secret")))
	current_app.web.audit(AuditAction.SECURITY_PROFILE_EDIT,
	                      object_type="SecurityProfile",
	                      object_id=profile_id)
	return flash_redirect("Security profile updated.", "security.security")


@bp.route("/<uuid:profile_id>/delete", methods=["POST"])
@login_required
def security_delete(profile_id: uuid.UUID) -> ResponseReturnValue:
	"""Delete a profile. The devices it was linked to need another profile
	of the user's before they roll out to them again (ACTION NEEDED): the
	links go with it."""
	with current_app.backend.postgres.get_session() as db_session:
		profile = SecurityProfiles(db_session, viewer()).owned(profile_id)
		if profile is None:
			return redirect(url_for("security.security"))
		label = profile.label or profile.username
		unlinked = db_session.query(DeviceCredential).filter_by(profile_id=profile_id).count()
		db_session.delete(profile)
	current_app.web.audit(AuditAction.SECURITY_PROFILE_DELETE, object_type="SecurityProfile",
	                      object_id=profile_id, object_label=label,
	                      detail={"devices_unlinked": unlinked})
	if unlinked:
		return flash_redirect(
			f"Profile deleted. {unlinked} device{'s' if unlinked != 1 else ''} now "
			f"need{'s' if unlinked == 1 else ''} your security profile.",
			"security.security", "warning")
	return flash_redirect("Profile deleted.", "security.security")


@bp.route("/<uuid:profile_id>/test", methods=["POST"])
@login_required
@with_json()
def security_test(profile_id: uuid.UUID, data: dict[str, Any]) -> ResponseReturnValue:
	"""Sign in with the profile (SSH, then disconnect) to a device the user
	sees - their own or a global one: JSON {device_id}; or to what the device
	window holds, saved or not: JSON {ip, port, device_type}.

	:returns: ok, or why not - 400 the window's fields, 401 refused, 503 the
	 port unreachable, 504 timed out"""
	try:
		device_obj = _test_target(profile_id, data)
		with _signing_in(device_obj), NetmikoSession.connect(device_obj):
			pass
	except SignInError as e:
		return err(str(e), e.code)
	return ok(f"Connected successfully to {device_obj.ip}")


@bp.route("/<uuid:profile_id>/detect", methods=["POST"])
@login_required
@with_json()
def security_detect(profile_id: uuid.UUID, data: dict[str, Any]) -> ResponseReturnValue:
	"""Sign in with the profile to the address the device window holds (JSON
	{ip, port}) and tell its platform (src/rollout/detect.py) - the window's
	Detect button. Checked and refused as Test sign-in is.

	:returns: ok {platform, detected, detected_name, hostname, evidence,
	 problem} (platform null: unsupported or unknown), or why not - 400 the
	 window's fields, 404 the profile, 401 refused, 503 unreachable, 504
	 timed out"""
	try:
		ip, port = _window_address(data, platform_required=False)
		with current_app.backend.postgres.get_session() as db_session:
			profile = SecurityProfiles(db_session, viewer()).owned(profile_id)
			if not profile:
				raise SignInError("Profile not found", 404)
			login = _login(profile)
		device_obj = _reachable(ip, port, "", ip, login)
		with _signing_in(device_obj):
			found = detect(device_obj)
	except SignInError as e:
		return err(str(e), e.code)
	return ok(platform=found.platform, detected=found.detected,
	          detected_name=NEIGHBOUR_NAMES.get(found.detected or ""), hostname=found.hostname,
	          evidence=found.evidence, problem=found.problem)


class SignInError(Exception):
	"""Why a sign-in to a device wasn't tried, or failed - in words, with
	its HTTP status (Test sign-in and Detect answer it)."""

	def __init__(self, message: str, code: int = 400) -> None:
		super().__init__(message)
		self.code = code


def _window_address(data: dict[str, Any], platform_required: bool) -> tuple[str, str]:
	"""The address and port the device window holds, checked (and its
	platform, for Test sign-in).

	:returns: (the address, normalised; the port)
	:raises SignInError: a field missing or invalid, or a loopback address"""
	ip = str(data.get("ip") or "").strip()
	port = str(data.get("port") or "").strip()
	if not ip:
		raise SignInError("Enter the device's IP address or hostname first")
	if not inputs.validate_address(ip):
		raise SignInError("Not a valid IP address or hostname")
	if not inputs.validate_port(port):
		raise SignInError("Port must be between 1 and 65535")
	if platform_required and not inputs.validate_platform(str(data.get("device_type") or "").strip()):
		raise SignInError("Choose the device's platform first")
	ip = inputs.normalize_address(ip)
	if is_loopback(ip):
		raise SignInError(own_server(ip))
	return ip, port


def _test_target(profile_id: uuid.UUID, data: dict[str, Any]) -> Device:
	"""Test sign-in's device: one the user sees (JSON {device_id}), or the
	device window's fields - with the user's own profile's login.

	:raises SignInError: why not"""
	device_id = None
	if "ip" in data:
		ip, port = _window_address(data, platform_required=True)
		device_type, label = str(data.get("device_type") or "").strip(), ip
	elif not data.get("device_id"):
		raise SignInError("No device selected", 404)
	else:
		try:
			device_id = uuid.UUID(str(data["device_id"]))
		except ValueError:
			raise SignInError("Invalid device ID", 422) from None

	with current_app.backend.postgres.get_session() as db_session:
		profile = SecurityProfiles(db_session, viewer()).owned(profile_id)
		if device_id:
			# any device the user sees: it's their own credentials being tried
			device = InventoryView(db_session, viewer()).get_visible(device_id)
			if not profile or not device:
				raise SignInError("Profile or device not found", 404)
			ip, port, device_type, label = device.ip, str(device.port), device.device_type, device.label
		elif not profile:
			raise SignInError("Profile not found", 404)
		login = _login(profile)
	return _reachable(ip, port, device_type, label, login)


def _login(profile: SecurityProfile) -> tuple[str, str, str]:
	""":returns: the profile's (username, password, enable secret or ""), decrypted"""
	secret = decrypt(profile.enable_secret) if profile.enable_secret else ""
	return profile.username, decrypt(profile.password_secret), secret


def _reachable(ip: str, port: str, device_type: str, label: str, login: tuple[str, str, str]) -> Device:
	"""The device to sign in to, once its name resolves (System Settings'
	DNS suffix added to a bare name) and its port answers.

	:raises SignInError: 503 - the name doesn't resolve, the port is closed"""
	suffix = current_app.web.dns_suffix()
	host = qualify(ip, suffix)
	if not is_ip(host) and resolve(host) is None:
		raise SignInError(f"Can't resolve {host} from this server", 503)
	if not inputs.tcp_reachable(host, int(port)):
		raise SignInError(f"TCP port {port} unreachable on {host}", 503)
	username, password, secret = login
	return Device(ip=ip, port=int(port), device_type=device_type, label=label,
	              username=username, password=password, secret=secret, dns_suffix=suffix)


@contextmanager
def _signing_in(device: Device) -> Iterator[None]:
	"""A sign-in's failure, in words: 401 refused, 504 timed out, else 500
	with the error's text.

	:raises SignInError: the sign-in failed"""
	try:
		yield
	except NetmikoAuthenticationException:
		raise SignInError("Authentication failed - check username and password", 401) from None
	except NetmikoTimeoutException:
		raise SignInError(f"Connection timed out on {device.endpoint}", 504) from None
	except Exception as e:
		raise SignInError(str(e), 500) from e
