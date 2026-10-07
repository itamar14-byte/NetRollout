"""The nightly clean-up (System Settings -> Retention: job records, config
snapshots, the audit log): the app runs the retention statements itself,
daily at 03:00 server time - on any PostgreSQL, nothing to install there. A
time missed while NetRollout was off is caught up once when it's back; a
failure is retried after an hour. The last outcome is kept in
config/retention-status.json for System Settings."""
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, time as clock, timedelta
from typing import Any

from sqlalchemy.engine import Engine

from src import runtime
from src.db.connections import BackendServices
from src.db.install import run_retention


RUN_AT = clock(3, 0)
CHECK_SECONDS = 60
RETRY = timedelta(hours=1)
STATUS_FILE = "retention-status.json"


def due(now: datetime, last_run: datetime | None) -> bool:
	"""03:00 has passed today and it hasn't run since (a server that was off
	then catches up once when it's back)."""
	today = datetime.combine(now.date(), RUN_AT)
	return now >= today and (last_run is None or last_run < today)


def read_status() -> dict[str, Any] | None:
	"""{time, ok, counts | message} of the last run, or None (never ran)."""
	try:
		return json.loads((runtime.config_dir() / STATUS_FILE).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return None


def _write_status(status: dict[str, Any]) -> None:
	"""Replace the status file (written aside, then renamed)."""
	folder = runtime.config_dir()
	folder.mkdir(parents=True, exist_ok=True)
	tmp = folder / (STATUS_FILE + ".tmp")
	tmp.write_text(json.dumps(status), encoding="utf-8")
	os.replace(tmp, folder / STATUS_FILE)


def run_once(engine: Engine, now: datetime | None = None) -> dict[str, Any]:
	"""The clean-up now; the outcome written for the page.

	:param now: the run's time (tests); now when None
	:returns: {time, ok, counts: {what: rows deleted}}
	:raises Exception: what the database raised (also written)"""
	now = now or datetime.now()
	stamp = now.isoformat(timespec="seconds")
	try:
		counts = run_retention(engine)
	except Exception as e:
		_write_status({"time": stamp, "ok": False, "message": str(e).splitlines()[0]})
		raise
	status = {"time": stamp, "ok": True, "counts": counts}
	_write_status(status)
	print("[NetRollout] nightly clean-up: " + ", ".join(f"{name} {n}" for name, n in counts.items()),
	      flush=True)
	return status


def _last_success() -> datetime | None:
	""":returns: when it last succeeded (the status file); None: never"""
	status = read_status()
	if status and status.get("ok"):
		try:
			return datetime.fromisoformat(status["time"])
		except (KeyError, ValueError):
			return None
	return None


def start_retention(backend: BackendServices,
                    hold: Callable[[], bool] = lambda: False) -> None:
	"""The daily clean-up, from a daemon thread (the database looked up each
	time: it follows a move). Called by the web app's entry point. Never raises.
	hold(): True while a database move runs - the clean-up waits for it."""
	def loop() -> None:
		last_run = _last_success()      # a restart doesn't run it again
		retry_at: datetime | None = None
		while True:
			time.sleep(CHECK_SECONDS)
			now = datetime.now()
			if (retry_at and now < retry_at) or not due(now, last_run) or hold():
				continue
			try:
				run_once(backend.postgres.engine, now)
				last_run, retry_at = now, None
			except Exception as e:          # noqa: BLE001 - keep the thread alive
				print(f"[NetRollout] ACTION NEEDED - the nightly clean-up failed: {e} "
				      f"(tried again in an hour)", flush=True)
				retry_at = now + RETRY
	threading.Thread(target=loop, name="retention", daemon=True).start()
