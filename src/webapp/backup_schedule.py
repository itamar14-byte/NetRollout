"""Scheduled backups (System Settings → Backups): the app makes them itself,
from a daemon thread, so they need nothing on the host. The engine is
src/backup.py.

Every CHECK_SECONDS the thread asks whether a scheduled time has passed
since the newest scheduled backup in the folder — a time missed while the
server was off is caught up when it's back, once. After a backup, retention
deletes the oldest scheduled ones beyond `backup_keep`. The outcome of the
last run is kept next to the backups (STATUS_FILE) for the page; a failure
is audited, printed as ACTION NEEDED and retried after RETRY_SECONDS.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from src import backup, runtime
from src.db.tables import AuditLog

CHECK_SECONDS = 30
FIRST_CHECK_SECONDS = 120     # not during the start itself
RETRY_SECONDS = 3600
STATUS_FILE = ".schedule-status.json"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ACTOR = "scheduler"


# ── When ─────────────────────────────────────────────────────────────────────

def _at(now: datetime, at: str) -> datetime:
	hour, minute = (int(x) for x in at.split(":"))
	return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def last_slot(now: datetime, schedule: str, at: str, weekday: str) -> datetime | None:
	"""The latest scheduled time at or before `now` (None: off)."""
	if schedule == "daily":
		slot = _at(now, at)
		return slot if slot <= now else slot - timedelta(days=1)
	if schedule == "weekly":
		slot = _at(now, at) - timedelta(days=(now.weekday() - WEEKDAYS.index(weekday)) % 7)
		return slot if slot <= now else slot - timedelta(days=7)
	return None


def next_slot(now: datetime, schedule: str, at: str, weekday: str) -> datetime | None:
	slot = last_slot(now, schedule, at, weekday)
	if slot is None:
		return None
	return slot + timedelta(days=1 if schedule == "daily" else 7)


def due(now: datetime, slot: datetime | None, newest: datetime | None,
        status: dict | None) -> bool:
	"""Whether to back up now: a scheduled time passed without a scheduled
	backup since, and no failure for it within the last RETRY_SECONDS."""
	if slot is None or (newest is not None and newest >= slot):
		return False
	if status and not status.get("ok"):
		failed = datetime.fromisoformat(status["time"])
		if failed >= slot and (now - failed).total_seconds() < RETRY_SECONDS:
			return False
	return True


def newest_scheduled(folder: Path) -> datetime | None:
	for entry in backup.list_backups(folder):          # newest first
		match = backup.NAME_RE.match(entry.name)
		if match["kind"] == "scheduled":
			return datetime.strptime(match["stamp"], "%Y%m%d-%H%M%S")
	return None


# ── The last outcome ─────────────────────────────────────────────────────────

def read_status(folder: Path | None = None) -> dict | None:
	try:
		return json.loads(((folder or runtime.backups_dir()) / STATUS_FILE)
		                  .read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


def _write_status(folder: Path, status: dict) -> None:
	folder.mkdir(parents=True, exist_ok=True)
	tmp = folder / (STATUS_FILE + ".tmp")
	tmp.write_text(json.dumps(status), encoding="utf-8")
	os.replace(tmp, folder / STATUS_FILE)


def system_audit(backend, action: str, *, label=None, success=True, detail=None):
	"""An audit row from the server itself (no request, no user)."""
	with backend.postgres.get_session() as db_session:
		db_session.add(AuditLog(actor_username=ACTOR, action=action,
		                        object_type="backup", object_label=label,
		                        success=success, detail=detail))


# ── Running ──────────────────────────────────────────────────────────────────

def run_scheduled(backend, now: datetime, places: backup.Places | None = None) -> dict:
	"""One scheduled backup, then retention; returns the status written."""
	places = places or backup.Places.app()
	try:
		path = backup.create(backend.postgres.engine, "scheduled", places, now=now)
		gone = backup.prune(places.backups, int(backend.settings.get("backup_keep")))
		status = {"time": now.isoformat(timespec="seconds"), "ok": True,
		          "file": path.name, "message": ""}
		system_audit(backend, "backup.created", label=path.name,
		             detail={"kind": "scheduled", "size": path.stat().st_size,
		                     "deleted": [p.name for p in gone]})
	except Exception as e:                  # noqa: BLE001 — reported, retried
		message = str(e).splitlines()[0] if str(e) else type(e).__name__
		status = {"time": now.isoformat(timespec="seconds"), "ok": False,
		          "file": None, "message": message}
		print(f"[NetRollout] ACTION NEEDED — scheduled backup failed: {message} "
		      f"(retried in {RETRY_SECONDS // 60} minutes)", flush=True)
		try:
			system_audit(backend, "backup.failed", success=False,
			             detail={"kind": "scheduled", "message": message})
		except Exception:                   # noqa: BLE001 — e.g. the database is down
			pass
	_write_status(places.backups, status)
	return status


def tick(backend, now: datetime | None = None,
         places: backup.Places | None = None) -> dict | None:
	"""Back up if one is due; returns the new status, or None if not due."""
	now = now or datetime.now()
	places = places or backup.Places.app()
	values = backend.settings.values()
	slot = last_slot(now, values["backup_schedule"], values["backup_time"],
	                 values["backup_weekday"])
	if not due(now, slot, newest_scheduled(places.backups),
	           read_status(places.backups)):
		return None
	return run_scheduled(backend, now, places)


def schedule_state(settings_values: dict, now: datetime | None = None) -> dict:
	"""For the page: the next time and the last outcome."""
	now = now or datetime.now()
	upcoming = next_slot(now, settings_values["backup_schedule"],
	                     settings_values["backup_time"],
	                     settings_values["backup_weekday"])
	return {"next": upcoming.isoformat(timespec="minutes") if upcoming else None,
	        "last": read_status()}


def start_backup_schedule(backend) -> None:
	"""The scheduler thread. Called by the web app's entry point. Never raises."""
	def loop():
		time.sleep(FIRST_CHECK_SECONDS)
		while True:
			try:
				tick(backend)
			except Exception as e:              # noqa: BLE001 — keep the thread alive
				print(f"[NetRollout] backup schedule check failed: {e}", flush=True)
			time.sleep(CHECK_SECONDS)
	threading.Thread(target=loop, name="backup-schedule", daemon=True).start()
