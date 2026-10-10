"""System Settings (admin panel → System). The registry and all validation
live in src/db/settings.py; these routes call it, audit every change, and
return per-field / rule errors for the page to show."""
from pathlib import Path
from typing import Any, cast

from flask import Blueprint, Response, jsonify, render_template, request, send_file
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

from src import runtime
from src.access.nginx import Verdict
from src.access.service import FOLLOWED, SaveResult
from src.audit import AuditAction
from src.backup import archive
from src.backup.schedule import schedule_state
from src.db import retention
from src.db.settings import (SETTINGS, Change, SettingsError, public_url,
                             rules_for_client)
from src.runtime import in_container
from src.webapp.app import current_app
from src.webapp.http import err, ok, require_admin, viewer, with_json
from src.webapp.startup import check_proxy, resolve_public_url


bp = Blueprint("admin_settings", __name__, url_prefix="/admin/settings")

RULES_KEY = "_rules"   # errors not tied to one field (cross-setting rules)


def _errors_response(e: SettingsError) -> tuple[Response, int]:
	""":returns: 422 with the errors by field (cross-setting rules under
	 RULES_KEY), for the page to show under each"""
	errors = {(k if k is not None else RULES_KEY): v for k, v in e.errors.items()}
	return jsonify({"status": "error", "message": str(e),
	                "errors": errors}), 422


def _state() -> dict[str, Any]:
	"""What the page needs after a change: every setting as displayed and
	which restart-only settings differ from what this process runs with."""
	settings = current_app.backend.settings
	return {
		"settings": settings.list_for_display(),
		"restart_pending": settings.restart_pending(
			current_app.settings_started_with),
		"port": current_app.access.port_state(),
		"access": current_app.access.overview(),
	}


def _audit(action: AuditAction, change: Change, proxy: Verdict | None = None,
           port: dict[str, Any] | None = None) -> None:
	"""One audit row per setting changed.

	:param proxy: nginx's verdict on a new hostname
	:param port: where a new port stands (Access.port_state)"""
	detail = {"key": change.key, "old": change.old, "new": change.new}
	if proxy:
		detail["nginx"] = proxy.state     # applied / not_managed / no_answer
	if port:
		detail["port_apply"] = port.get("state")  # manual / waiting / applied
	current_app.web.audit(action, object_type="SystemSetting",
	                      object_label=change.key, detail=detail)


@bp.app_context_processor
def restart_pending_for_admin_pages() -> dict[str, Any]:
	"""Every admin page's Restart button shows the orange dot while a
	restart-only setting differs from what this process runs with — decided
	on the server, so it survives page loads."""
	if not (request.path.startswith("/admin") and current_user.is_authenticated
	        and viewer().is_admin):
		return {}
	try:
		pending = current_app.backend.settings.restart_pending(
			current_app.settings_started_with)
	except SQLAlchemyError:
		pending = []
	return {"settings_restart_pending": pending}


@bp.route("")
@login_required
@require_admin
def settings_page() -> str:
	"""System Settings: every card, the rules (checked in the page too), the
	port and access state, backups and the last clean-up."""
	state = _state()
	cards = list(dict.fromkeys(s.card for s in SETTINGS.values()))
	return render_template("admin_settings.html", active_section="settings",
	                       cards=cards, settings=state["settings"],
	                       rules=rules_for_client(), port=state["port"],
	                       access=state["access"], backups=backups_state(),
	                       cleanup=retention.read_status(),
	                       app_port=current_app.app_port)


def _save(values: dict[str, Any], action: AuditAction) -> ResponseReturnValue:
	"""Save through Access.save - all or nothing, nginx's verdict included.

	:param values: setting key → the value typed
	:param action: the audit action (settings.update / settings.reset)
	:returns: the changes and the page's new state; or 422 with the reasons"""
	try:
		result = current_app.access.save(values, current_user.id)
	except SettingsError as e:
		return _errors_response(e)
	return _saved(result, action)


def _saved(result: SaveResult, action: AuditAction) -> ResponseReturnValue:
	""":returns: the changes, nginx's verdict and the page's new state - each
	 change audited"""
	state = _state()
	for change in result.changes:
		_audit(action, change,
		       proxy=result.proxy if change.key == "public_hostname" else None,
		       port=state["port"] if change.key == "https_port" else None)
	return ok(changed=[c.key for c in result.changes],
	          proxy=result.proxy.as_dict() if result.proxy else None, **state)


@bp.route("", methods=["POST"])
@login_required
@require_admin
@with_json("values")
def settings_save(data: dict[str, Any]) -> ResponseReturnValue:
	"""Save every edited field at once — all or nothing."""
	values = data["values"]
	if not isinstance(values, dict):
		return err("Invalid request")
	return _save(values, AuditAction.SETTINGS_UPDATE)


@bp.route("/<key>/reset", methods=["POST"])
@login_required
@require_admin
def settings_reset(key: str) -> ResponseReturnValue:
	"""Back to a setting's default - the hostname and the port through
	_save, as nginx and the port helper follow them."""
	if key not in SETTINGS or not SETTINGS[key].editable:
		return err("Unknown setting", 404)
	try:
		result = current_app.access.reset(key, current_user.id)
	except SettingsError as e:
		return _errors_response(e)
	if key in FOLLOWED:   # nginx and the port helper follow: the full answer
		return _saved(result, AuditAction.SETTINGS_RESET)
	for change in result.changes:
		_audit(AuditAction.SETTINGS_RESET, change)
	return ok(changed=[key] if result.changes else [], **_state())


def _reached_port() -> int:
	"""The port this request came through (nginx forwards the browser's Host
	header: "name:port", no port = 443)."""
	host = request.host
	port = host.rsplit(":", 1)[1] if ":" in host and not host.endswith("]") else ""
	return int(port) if port.isascii() and port.isdigit() else 443


@bp.route("/port")
@login_required
@require_admin
def port_status() -> Response:
	"""Where a port change stands — polled by the page (as a background
	request: it must not keep a session alive)."""
	return ok(port=current_app.access.port_state())


@bp.route("/port/confirm", methods=["POST"])
@login_required
@require_admin
@with_json("id")
def port_confirm(data: dict[str, Any]) -> ResponseReturnValue:
	"""Sent by the page served on the new port: this admin reached it, so the
	helper may drop the old one."""
	problem = current_app.access.confirm_port(str(data["id"]), _reached_port())
	if problem:
		return err(problem, 409)
	current_app.web.audit(AuditAction.SETTINGS_PORT_CONFIRMED, object_type="SystemSetting",
	                      object_label="https_port",
	                      detail={"port": _reached_port()})
	return ok(port=current_app.access.port_state())


@bp.route("/port/retry", methods=["POST"])
@login_required
@require_admin
def port_retry() -> ResponseReturnValue:
	"""Ask the helper again for the saved port (after a rollback — e.g. the
	firewall was opened meanwhile)."""
	try:
		current_app.access.retry_port()
	except OSError as e:
		return err(f"NetRollout couldn't write {e.filename or 'config/'}: "
		           f"{e.strerror or e}.", 500)
	return ok(port=current_app.access.port_state())


@bp.route("/test", methods=["POST"])
@login_required
@require_admin
@with_json()
def settings_test_access(data: dict[str, Any]) -> ResponseReturnValue:
	"""Run the startup reverse-proxy check against the hostname/port typed on
	the page (saved or not). Admin-only: it makes the server fetch an address
	the admin chose — https only, 2 s timeouts."""
	try:
		hostname = cast(str, SETTINGS["public_hostname"].parse(data.get("hostname", "")))
		port = cast(int, SETTINGS["https_port"].parse(data.get("port", 443)))
	except ValueError as e:
		return err(str(e), 422)
	url, source = resolve_public_url(public_url(hostname, port))
	if in_container():
		# From inside the container the published port isn't reliably
		# reachable: a probe here would report working setups as broken —
		# what nginx itself last reported is shown instead
		return ok(url=url, source=source, container=True,
		          access=current_app.access.overview())
	local, public = check_proxy(url, current_app.instance_token)
	return ok(url=url, source=source,
	          local={"ok": local.ok, "reason": local.reason},
	          public={"ok": public.ok, "reason": public.reason})


# ══ Backups: /admin/backups (System Settings' Backups card) ══════════════════

backups_bp = Blueprint("admin_backups", __name__, url_prefix="/admin/backups")


def backups_state() -> dict[str, Any]:
	"""What the Backups card shows: the files, the next time, the last
	scheduled outcome."""
	entries: list[dict[str, Any]] = []
	for e in archive.BackupFolder.app().entries():
		m = e.manifest
		entries.append({"name": e.name, "size": e.size, "kind": e.kind,
		                "created": m.created if m else None,
		                "version": m.version if m else None,
		                "problem": e.problem})
	return {"backups": entries, "total": sum(e["size"] for e in entries),
	        **schedule_state(current_app.backend.settings.values())}


def _file(name: str) -> Path | None:
	"""A backup in the folder by its exact name — never a path elsewhere.

	:returns: its path; None: no such backup"""
	if not archive.NAME_RE.match(name):
		return None
	path = runtime.backups_dir() / name
	return path if path.is_file() else None


def _audit_backup(action: AuditAction, name: str, **detail: Any) -> None:
	current_app.web.audit(action, object_type="backup", object_label=name,
	                      detail=detail or None)


@backups_bp.route("")
@login_required
@require_admin
def backups_list() -> Response:
	""":returns: the Backups card's state (backups_state)"""
	return ok(**backups_state())


@backups_bp.route("", methods=["POST"])
@login_required
@require_admin
def backups_create() -> ResponseReturnValue:
	"""Back up now. Runs in this request: seconds for most installations."""
	try:
		path = archive.create(current_app.backend.postgres.engine, archive.BackupKind.MANUAL)
	except archive.BackupError as e:
		current_app.web.audit(AuditAction.BACKUP_FAILED, object_type="backup", success=False,
		                      detail={"kind": archive.BackupKind.MANUAL, "message": str(e)})
		return err(str(e), 409)
	_audit_backup(AuditAction.BACKUP_CREATED, path.name, kind=archive.BackupKind.MANUAL, size=path.stat().st_size)
	return ok(created=path.name, **backups_state())


@backups_bp.route("/<name>")
@login_required
@require_admin
def backups_download(name: str) -> ResponseReturnValue:
	"""The zip holds the encryption key: admins only, and recorded."""
	path = _file(name)
	if path is None:
		return err("No such backup", 404)
	_audit_backup(AuditAction.BACKUP_DOWNLOADED, name)
	return send_file(path, as_attachment=True, download_name=name,
	                 mimetype="application/zip")


@backups_bp.route("/<name>/delete", methods=["POST"])
@login_required
@require_admin
def backups_delete(name: str) -> ResponseReturnValue:
	"""Delete a backup (audited).

	:returns: the card's new state, or 404"""
	path = _file(name)
	if path is None:
		return err("No such backup", 404)
	path.unlink(missing_ok=True)
	_audit_backup(AuditAction.BACKUP_DELETED, name)
	return ok(**backups_state())
