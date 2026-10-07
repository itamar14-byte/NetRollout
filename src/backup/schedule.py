"""Scheduled backups (System Settings → Backups): the app makes them itself,
from a daemon thread, so they need nothing on the host. The engine is
src/backup/archive.py.

Every CHECK_SECONDS the thread asks whether a scheduled time has passed
since the newest scheduled backup in the folder — a time missed while the
server was off is caught up when it's back, once. After a backup, retention
deletes the oldest scheduled ones beyond `backup_keep`. The outcome of the
last run is kept next to the backups (STATUS_FILE) for the page; a failure
is audited, printed as ACTION NEEDED and retried after RETRY_SECONDS.
"""
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src import runtime
from src.backup import archive
from src.db.connections import BackendServices
from src.db.tables import AuditLog


CHECK_SECONDS = 30
FIRST_CHECK_SECONDS = 120     # not during the start itself
RETRY_SECONDS = 3600
STATUS_FILE = ".schedule-status.json"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ACTOR = "scheduler"


# ── When ─────────────────────────────────────────────────────────────────────

def _at(now: datetime, at: str) -> datetime:
	""":returns: `now`'s day at the time `at` ("HH:MM")"""
	hour, minute = (int(x) for x in at.split(":"))
	return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def last_slot(now: datetime, schedule: str, at: str, weekday: str) -> datetime | None:
	"""The latest scheduled time at or before `now`.

	:param schedule: off / daily / weekly
	:param at: the time of day, "HH:MM"
	:param weekday: weekly's day, one of WEEKDAYS
	:returns: that time; None when off"""
	if schedule == "daily":
		slot = _at(now, at)
		return slot if slot <= now else slot - timedelta(days=1)
	if schedule == "weekly":
		slot = _at(now, at) - timedelta(days=(now.weekday() - WEEKDAYS.index(weekday)) % 7)
		return slot if slot <= now else slot - timedelta(days=7)
	return None


def next_slot(now: datetime, schedule: str, at: str, weekday: str) -> datetime | None:
	""":returns: the next scheduled time after `now` (as last_slot); None: off"""
	slot = last_slot(now, schedule, at, weekday)
	if slot is None:
		return None
	return slot + timedelta(days=1 if schedule == "daily" else 7)


def due(now: datetime, slot: datetime | None, newest: datetime | None,
        status: dict[str, Any] | None) -> bool:
	"""Whether to back up now: a scheduled time passed without a scheduled
	backup since, and no failure for it within the last RETRY_SECONDS.

	:param slot: the latest scheduled time (last_slot)
	:param newest: the newest scheduled backup's time
	:param status: the last run's outcome"""
	if slot is None or (newest is not None and newest >= slot):
		return False
	if status and not status.get("ok"):
		failed = datetime.fromisoformat(status["time"])
		if failed >= slot and (now - failed).total_seconds() < RETRY_SECONDS:
			return False
	return True


def newest_scheduled(folder: Path) -> datetime | None:
	""":returns: when the newest scheduled backup in the folder was made;
	 None: there's none"""
	for entry in archive.list_backups(folder):          # newest first
		if entry.kind == "scheduled":
			return datetime.strptime(entry.stamp, "%Y%m%d-%H%M%S")
	return None


# ── The last outcome ─────────────────────────────────────────────────────────

def read_status(folder: Path | None = None) -> dict[str, Any] | None:
	""":param folder: the backups folder; the app's when None
	:returns: the last run's {time, ok, file, message}; None: never ran"""
	try:
		return json.loads(((folder or runtime.backups_dir()) / STATUS_FILE)
		                  .read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


def system_audit(backend: BackendServices, action: str, *, label: str | None = None,
                 success: bool = True, detail: dict[str, Any] | None = None) -> None:
	"""An audit row from the server itself (no request, no user)."""
	with backend.postgres.get_session() as db_session:
		db_session.add(AuditLog(actor_username=ACTOR, action=action,
		                        object_type="backup", object_label=label,
		                        success=success, detail=detail))


# ── Running ──────────────────────────────────────────────────────────────────

def run_scheduled(backend: BackendServices, now: datetime,
                  places: archive.Places | None = None) -> dict[str, Any]:
	"""One scheduled backup, then retention (backup_keep). A failure is
	reported (ACTION NEEDED, audited), never raised.

	:param places: the folders; the app's when None
	:returns: the status written"""
	places = places or archive.Places.app()
	try:
		path = archive.create(backend.postgres.engine, "scheduled", places, now=now)
		gone = archive.prune(places.backups, int(backend.settings.get("backup_keep")))
		status: dict[str, Any] = {"time": now.isoformat(timespec="seconds"), "ok": True,
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
	runtime.write_json(places.backups / STATUS_FILE, status)
	return status


def tick(backend: BackendServices, now: datetime | None = None,
         places: archive.Places | None = None) -> dict[str, Any] | None:
	"""Back up if one is due.

	:returns: the new status; None if none was due"""
	now = now or datetime.now()
	places = places or archive.Places.app()
	values = backend.settings.values()
	slot = last_slot(now, values["backup_schedule"], values["backup_time"],
	                 values["backup_weekday"])
	if not due(now, slot, newest_scheduled(places.backups),
	           read_status(places.backups)):
		return None
	return run_scheduled(backend, now, places)


def schedule_state(settings_values: dict[str, Any],
                   now: datetime | None = None) -> dict[str, Any]:
	"""For the page: the next time and the last outcome.

	:param settings_values: the System Settings (backup_schedule, _time,
	 _weekday)
	:returns: {"next": ISO time or None (off), "last": read_status()}"""
	now = now or datetime.now()
	upcoming = next_slot(now, settings_values["backup_schedule"],
	                     settings_values["backup_time"],
	                     settings_values["backup_weekday"])
	return {"next": upcoming.isoformat(timespec="minutes") if upcoming else None,
	        "last": read_status()}


def start_backup_schedule(backend: BackendServices,
                          hold: Callable[[], bool] = lambda: False) -> None:
	"""The scheduler thread. Called by the web app's entry point. Never raises.
	hold(): True while a database move runs - a due backup waits (caught up
	after it; the move itself takes the backup lock)."""
	def loop() -> None:
		time.sleep(FIRST_CHECK_SECONDS)
		while True:
			try:
				if not hold():
					tick(backend)
			except Exception as e:              # noqa: BLE001 — keep the thread alive
				print(f"[NetRollout] backup schedule check failed: {e}", flush=True)
			time.sleep(CHECK_SECONDS)
	threading.Thread(target=loop, name="backup-schedule", daemon=True).start()
