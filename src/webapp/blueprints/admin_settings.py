"""System Settings (admin panel → System). The registry and all validation
live in src/db/settings.py; these routes call it, audit every change, and
return per-field / rule errors for the page to show."""
import time

# flask
from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required
# sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

# local modules
from src.db.settings import (SETTINGS, SettingsError, public_url,
                             rules_for_client)
from src.runtime import in_container
from src.webapp import proxy_config
from src.webapp.startup import check_proxy, resolve_public_url
from src.webapp.utils import err, ok, require_admin, with_json

bp = Blueprint("admin_settings", __name__, url_prefix="/admin/settings")

RULES_KEY = "_rules"   # errors not tied to one field (cross-setting rules)


##############################Route Helpers####################################
def _errors_response(e: SettingsError):
	errors = {(k if k is not None else RULES_KEY): v for k, v in e.errors.items()}
	return jsonify({"status": "error", "message": str(e),
	                "errors": errors}), 422


def _state():
	"""What the page needs after a change: every setting as displayed and
	which restart-only settings differ from what this process runs with."""
	settings = current_app.backend.settings
	return {
		"settings": settings.list_for_display(),
		"restart_pending": settings.restart_pending(
			current_app.config.get("SETTINGS_STARTED_WITH", {})),
	}


def _audit(action, change, proxy=None):
	detail = {"key": change.key, "old": change.old, "new": change.new}
	if proxy:
		detail["nginx"] = proxy.get("state")     # applied / not_managed / no_answer
	current_app.web.audit(action, object_type="SystemSetting",
	                      object_label=change.key, detail=detail)


@bp.app_context_processor
def restart_pending_for_admin_pages():
	"""Every admin page's Restart button shows the orange dot while a
	restart-only setting differs from what this process runs with — decided
	on the server, so it survives page loads."""
	if not (request.path.startswith("/admin") and current_user.is_authenticated
	        and current_user.role == "admin"):
		return {}
	try:
		pending = current_app.backend.settings.restart_pending(
			current_app.config.get("SETTINGS_STARTED_WITH", {}))
	except SQLAlchemyError:
		pending = []
	return {"settings_restart_pending": pending}


##############################Routes#######################################
@bp.route("")
@login_required
@require_admin
def settings_page():
	state = _state()
	cards = list(dict.fromkeys(s.card for s in SETTINGS.values()))
	return render_template("admin_settings.html", active_section="settings",
	                       cards=cards, settings=state["settings"],
	                       rules=rules_for_client(),
	                       app_port=current_app.config.get("APP_PORT"))


def _save(values: dict, action: str):
	"""Validate, prepare nginx for a new hostname, save, and report nginx's
	verdict — all or nothing: an invalid value, a certificate that doesn't
	cover the new name, a file that can't be written, or nginx rejecting the
	result leaves every setting and file as it was, and says why."""
	store = current_app.backend.settings
	try:
		changes = store.plan(values)
	except SettingsError as e:
		return _errors_response(e)
	host = next((c for c in changes if c.key == "public_hostname"), None)
	undo, proxy = None, None
	if host:
		managed = proxy_config.read_status() is not None
		started = time.time()
		try:
			undo = proxy_config.change_hostname(host.new)
		except proxy_config.ProxyError as e:
			return _errors_response(SettingsError({"public_hostname": str(e)}))
	try:
		changes = store.update(values, current_user.id)
	except SettingsError as e:            # changed meanwhile: back out
		if undo:
			undo()
		return _errors_response(e)
	if host:
		proxy = _verdict(managed, started, host.new)
		if proxy["state"] == "rejected":
			store.update({"public_hostname": host.old}, current_user.id)
			undo()
			return _errors_response(SettingsError({"public_hostname":
				f"nginx rejected the new hostname: {proxy.get('message')} — "
				f"nothing was changed, the previous hostname is back."}))
	for change in changes:
		_audit(action, change,
		       proxy=proxy if change.key == "public_hostname" else None)
	return ok(changed=[c.key for c in changes], proxy=proxy, **_state())


def _verdict(managed: bool, started: float, hostname: str) -> dict:
	"""nginx's answer for the new hostname: applied / rejected, or why
	there is none."""
	if not managed:
		return {"state": "not_managed"}
	status = proxy_config.wait_for_status(started, hostname=hostname)
	if status is None:
		return {"state": "no_answer"}
	return {"state": status.get("state"), "message": status.get("message")}


@bp.route("", methods=["POST"])
@login_required
@require_admin
@with_json("values")
def settings_save(data):
	"""Save every edited field at once — all or nothing."""
	values = data["values"]
	if not isinstance(values, dict):
		return err("Invalid request")
	return _save(values, "settings.update")


@bp.route("/<key>/reset", methods=["POST"])
@login_required
@require_admin
def settings_reset(key):
	if key not in SETTINGS or not SETTINGS[key].editable:
		return err("Unknown setting", 404)
	if key == "public_hostname":          # nginx follows it: the full path
		return _save({key: SETTINGS[key].default}, "settings.reset")
	try:
		change = current_app.backend.settings.reset(key, current_user.id)
	except SettingsError as e:
		return _errors_response(e)
	if change:
		_audit("settings.reset", change)
	return ok(changed=[key] if change else [], **_state())


@bp.route("/test", methods=["POST"])
@login_required
@require_admin
@with_json()
def settings_test_access(data):
	"""Run the startup reverse-proxy check against the hostname/port typed on
	the page (saved or not). Admin-only: it makes the server fetch an address
	the admin chose — https only, 2 s timeouts."""
	try:
		hostname = SETTINGS["public_hostname"].parse(data.get("hostname", ""))
		port = SETTINGS["https_port"].parse(data.get("port", 443))
	except ValueError as e:
		return err(str(e), 422)
	url, source = resolve_public_url(public_url(hostname, port))
	if in_container():
		# From inside the container the published port isn't reliably
		# reachable: a probe here would report working setups as broken
		return ok(url=url, source=source, container=True)
	if not url:
		return ok(url=None, source=source, local=None, public=None)
	local, public = check_proxy(url, current_app.config["INSTANCE_TOKEN"])
	return ok(url=url, source=source,
	          local={"ok": local.ok, "reason": local.reason},
	          public={"ok": public.ok, "reason": public.reason})
