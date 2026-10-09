"""Scheduled backups (System Settings → Backups): the app makes them itself,
from a daemon thread, so they need nothing on the host. The engine is
src/backup/archive.py.

Every CHECK_SECONDS the thread asks whether a scheduled time has passed
since the newest scheduled backup in the folder — a time missed while the
server was off is caught up when it's back, once. After a backup, retention
deletes the oldest scheduled ones beyond `backup_keep`. The outcome of the
last run is kept next to the backups (BackupFolder.status) for the page; a failure
is audited, printed as ACTION NEEDED and retried after RETRY_SECONDS.
"""
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from src.audit import Actor, AuditAction
from src.backup import archive
from src.db.connections import BackendServices
from src.db.settings import BackupSchedule, Weekday


CHECK_SECONDS = 30
FIRST_CHECK_SECONDS = 120     # not during the start itself
RETRY_SECONDS = 3600
WEEKDAYS = tuple(Weekday)       # Monday first, as datetime.weekday()
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
	if schedule == BackupSchedule.DAILY:
		slot = _at(now, at)
		return slot if slot <= now else slot - timedelta(days=1)
	if schedule == BackupSchedule.WEEKLY:
		slot = _at(now, at) - timedelta(days=(now.weekday() - WEEKDAYS.index(weekday)) % 7)
		return slot if slot <= now else slot - timedelta(days=7)
	return None


def next_slot(now: datetime, schedule: str, at: str, weekday: str) -> datetime | None:
	""":returns: the next scheduled time after `now` (as last_slot); None: off"""
	slot = last_slot(now, schedule, at, weekday)
	if slot is None:
		return None
	return slot + timedelta(days=1 if schedule == BackupSchedule.DAILY else 7)


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


# ── The audit ────────────────────────────────────────────────────────────────

def system_audit(backend: BackendServices, action: AuditAction, *, label: str | None = None,
                 success: bool = True, detail: dict[str, Any] | None = None) -> None:
	"""A backup's audit row from the server itself (no request, no user)."""
	backend.audit_trail.record(Actor.system(ACTOR), action, object_type="backup",
	                           object_label=label, success=success, detail=detail)


# ── Running ──────────────────────────────────────────────────────────────────

def run_scheduled(backend: BackendServices, now: datetime,
                  places: archive.Places | None = None) -> dict[str, Any]:
	"""One scheduled backup, then retention (backup_keep). A failure is
	reported (ACTION NEEDED, audited), never raised.

	:param places: the folders; the app's when None
	:returns: the status written"""
	places = places or archive.Places.app()
	folder = archive.BackupFolder(places.backups)
	try:
		path = archive.create(backend.postgres.engine, archive.BackupKind.SCHEDULED, places, now=now)
		gone = folder.prune(int(backend.settings.get("backup_keep")))
		status: dict[str, Any] = {"time": now.isoformat(timespec="seconds"), "ok": True,
		          "file": path.name, "message": ""}
		system_audit(backend, AuditAction.BACKUP_CREATED, label=path.name,
		             detail={"kind": archive.BackupKind.SCHEDULED, "size": path.stat().st_size,
		                     "deleted": [p.name for p in gone]})
	except Exception as e:                  # noqa: BLE001 — reported, retried
		message = str(e).splitlines()[0] if str(e) else type(e).__name__
		status = {"time": now.isoformat(timespec="seconds"), "ok": False,
		          "file": None, "message": message}
		print(f"[NetRollout] ACTION NEEDED — scheduled backup failed: {message} "
		      f"(retried in {RETRY_SECONDS // 60} minutes)", flush=True)
		try:
			system_audit(backend, AuditAction.BACKUP_FAILED, success=False,
			             detail={"kind": archive.BackupKind.SCHEDULED, "message": message})
		except Exception:                   # noqa: BLE001 — e.g. the database is down
			pass
	folder.write_status(status)
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
	folder = archive.BackupFolder(places.backups)
	if not due(now, slot, folder.newest_scheduled(), folder.status()):
		return None
	return run_scheduled(backend, now, places)


def schedule_state(settings_values: dict[str, Any],
                   now: datetime | None = None) -> dict[str, Any]:
	"""For the page: the next time and the last outcome.

	:param settings_values: the System Settings (backup_schedule, _time,
	 _weekday)
	:returns: {"next": ISO time or None (off), "last": the app's folder's
	 BackupFolder.status()}"""
	now = now or datetime.now()
	upcoming = next_slot(now, settings_values["backup_schedule"],
	                     settings_values["backup_time"],
	                     settings_values["backup_weekday"])
	return {"next": upcoming.isoformat(timespec="minutes") if upcoming else None,
	        "last": archive.BackupFolder.app().status()}


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
