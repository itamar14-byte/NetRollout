"""System Settings (admin panel → System). The registry and all validation
live in src/db/settings.py; these routes call it, audit every change, and
return per-field / rule errors for the page to show."""
# flask
from flask import Blueprint, current_app, jsonify
from flask_login import current_user, login_required

# local modules
from src.db.settings import SETTINGS, SettingsError, public_url
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


def _audit(action, change):
	current_app.web.audit(action, object_type="SystemSetting",
	                      object_label=change.key,
	                      detail={"key": change.key, "old": change.old,
	                              "new": change.new})


##############################Routes#######################################
@bp.route("", methods=["POST"])
@login_required
@require_admin
@with_json("values")
def settings_save(data):
	"""Save every edited field at once — all or nothing."""
	values = data["values"]
	if not isinstance(values, dict):
		return err("Invalid request")
	try:
		changes = current_app.backend.settings.update(values, current_user.id)
	except SettingsError as e:
		return _errors_response(e)
	for change in changes:
		_audit("settings.update", change)
	return ok(changed=[c.key for c in changes], **_state())


@bp.route("/<key>/reset", methods=["POST"])
@login_required
@require_admin
def settings_reset(key):
	if key not in SETTINGS or not SETTINGS[key].editable:
		return err("Unknown setting", 404)
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
	if not url:
		return ok(url=None, source=source, local=None, public=None)
	local, public = check_proxy(url, current_app.config["INSTANCE_TOKEN"])
	return ok(url=url, source=source,
	          local={"ok": local.ok, "reason": local.reason},
	          public={"ok": public.ok, "reason": public.reason})
