"""Security profiles: the credentials devices are reached with (stored
encrypted: SecurityProfiles in src/inventory.py) - create, edit, delete, and
test one against a device."""
import uuid
from typing import Any

from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask.typing import ResponseReturnValue
from flask_login import login_required
from netmiko import NetmikoAuthenticationException, NetmikoTimeoutException

from src.audit import AuditAction
from src.db.tables import DeviceCredential, Inventory
from src.encryption import decrypt
from src.inventory import InventoryView, SecurityProfiles
from src.rollout import inputs
from src.rollout.engine import Device
from src.rollout.session import NetmikoSession
from src.webapp.app import current_app
from src.webapp.hooks import signed_in_user
from src.webapp.http import ok, err, with_json, with_form, flash_redirect, viewer

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
			f"need{'s' if unlinked == 1 else ''} a security profile of yours.",
			"security.security", "warning")
	return flash_redirect("Profile deleted.", "security.security")


@bp.route("/<uuid:profile_id>/test", methods=["POST"])
@login_required
@with_json()
def security_test(profile_id: uuid.UUID, data: dict[str, Any]) -> ResponseReturnValue:
	"""Sign in with the profile to a device the user sees - their own or a
	global one (SSH, then disconnect): JSON {device_id}.

	:returns: ok, or why not - 401 refused, 503 the port unreachable, 504
	 timed out"""
	if not data.get("device_id"):
		return err("No device selected", 404)

	try:
		device_id = uuid.UUID(str(data["device_id"]))
	except ValueError:
		return err("Invalid device ID", 422)

	with current_app.backend.postgres.get_session() as db_session:
		profile = SecurityProfiles(db_session, viewer()).owned(profile_id)
		# any device the user sees: it's their own credentials being tried
		device = InventoryView(db_session, viewer()).get_visible(device_id)
		if not profile or not device:
			return err("Profile or device not found", 404)
		if not inputs.tcp_reachable(device.ip, device.port):
			return err(f"TCP port {device.port} unreachable on {device.ip}",
			           503)
		db_session.expunge_all()

	device_obj = Device(ip=device.ip,
	                    port=device.port,
	                    device_type=device.device_type,
	                    label=device.label,
	                    username=profile.username,
	                    password=decrypt(profile.password_secret),
	                    secret=decrypt(profile.enable_secret)
	                    if profile.enable_secret else ""
	                    )
	try:
		with NetmikoSession.connect(device_obj):
			pass
		return ok(f"Connected successfully to {device.ip}")
	except NetmikoAuthenticationException:
		return err("Authentication failed — check username and password", 401)
	except NetmikoTimeoutException:
		return err(f"Connection timed out on {device_obj.endpoint}", 504)
	except Exception as e:
		return err(str(e), 500)
