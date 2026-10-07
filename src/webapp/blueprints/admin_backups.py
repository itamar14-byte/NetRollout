"""Backups (System Settings → Backups): the list, Back up now, Download,
Delete — admins only, every action audited. The schedule and retention are
ordinary System Settings; restoring stops the app, so it runs on the server
(NetRollout Manager → Restore…, `netrollout restore`). Engine: src/backup/archive.py."""
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, send_file
from flask.typing import ResponseReturnValue
from flask_login import login_required

from src import runtime
from src.backup import archive as backup
from src.backup.schedule import schedule_state
from src.webapp.flask_app import current_app
from src.webapp.utils import err, ok, require_admin

bp = Blueprint("admin_backups", __name__, url_prefix="/admin/backups")


def backups_state() -> dict[str, Any]:
	"""What the Backups card shows: the files, the next time, the last
	scheduled outcome."""
	entries: list[dict[str, Any]] = []
	for e in backup.list_backups(runtime.backups_dir()):
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
	if not backup.NAME_RE.match(name):
		return None
	path = runtime.backups_dir() / name
	return path if path.is_file() else None


def _audit(action: str, name: str, **detail: Any) -> None:
	current_app.web.audit(action, object_type="backup", object_label=name,
	                      detail=detail or None)


@bp.route("")
@login_required
@require_admin
def backups_list() -> Response:
	""":returns: the Backups card's state (backups_state)"""
	return ok(**backups_state())


@bp.route("", methods=["POST"])
@login_required
@require_admin
def backups_create() -> ResponseReturnValue:
	"""Back up now. Runs in this request: seconds for most installations."""
	try:
		path = backup.create(current_app.backend.postgres.engine, "manual")
	except backup.BackupError as e:
		current_app.web.audit("backup.failed", object_type="backup", success=False,
		                      detail={"kind": "manual", "message": str(e)})
		return err(str(e), 409)
	_audit("backup.created", path.name, kind="manual", size=path.stat().st_size)
	return ok(created=path.name, **backups_state())


@bp.route("/<name>")
@login_required
@require_admin
def backups_download(name: str) -> ResponseReturnValue:
	"""The zip holds the encryption key: admins only, and recorded."""
	path = _file(name)
	if path is None:
		return err("No such backup", 404)
	_audit("backup.downloaded", name)
	return send_file(path, as_attachment=True, download_name=name,
	                 mimetype="application/zip")


@bp.route("/<name>/delete", methods=["POST"])
@login_required
@require_admin
def backups_delete(name: str) -> ResponseReturnValue:
	"""Delete a backup (audited).

	:returns: the card's new state, or 404"""
	path = _file(name)
	if path is None:
		return err("No such backup", 404)
	path.unlink(missing_ok=True)
	_audit("backup.deleted", name)
	return ok(**backups_state())
