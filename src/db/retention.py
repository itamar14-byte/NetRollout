"""The nightly clean-up (System Settings -> Retention: job records, config
snapshots, the audit log): the app runs the retention statements itself,
daily at 03:00 server time - on any PostgreSQL, nothing to install there. A
time missed while NetRollout was off is caught up once when it's back; a
failure is retried after an hour. The last outcome is kept in
config/retention-status.json for System Settings."""
from collections.abc import Callable
from datetime import datetime, time as clock, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src import runtime
from src.db.connections import BackendServices
from src.db.settings import sql_value


RUN_AT = clock(3, 0)
CHECK_SECONDS = 60
RETRY = timedelta(hours=1)
STATUS_FILE = "retention-status.json"


def _older_than(column: str, setting: str) -> str:
	"""SQL: `column` is older than the setting's number of days (read from
	system_settings when the statement runs)."""
	return f"{column} < NOW() - make_interval(days => {sql_value(setting)})"


RETENTION_STATEMENTS = {
	"device_result_retention":
		f"DELETE FROM device_results "
		f"WHERE {_older_than('completed_at', 'job_retention_days')}",
	# created_at is submission time, results age from completion — only
	# delete metadata once the job's results are gone, so both expire together
	"job_metadata_retention":
		f"DELETE FROM job_metadata m "
		f"WHERE {_older_than('m.created_at', 'job_retention_days')} "
		f"AND NOT EXISTS (SELECT 1 FROM device_results r "
		f"WHERE r.job_id = m.job_id)",
	# clear the payload, keep the row (status/analytics survive)
	"device_result_config_retention":
		f"UPDATE device_results SET fetched_config = NULL "
		f"WHERE fetched_config IS NOT NULL AND "
		f"{_older_than('completed_at', 'config_snapshot_retention_days')}",
	"audit_log_retention":
		f"DELETE FROM audit_log "
		f"WHERE {_older_than('timestamp', 'audit_retention_days')}",
}


def run_retention(engine: Engine) -> dict[str, int]:
	"""The retention statements, once, in one transaction (NightlyCleanUp runs
	them daily). Each reads its period from
	system_settings.

	:param engine: the database NetRollout uses
	:returns: the rows each statement touched, by its name
	:raises sqlalchemy.exc.SQLAlchemyError: the database failed - nothing changed"""
	with engine.begin() as conn:
		return {name: conn.execute(text(statement)).rowcount
		        for name, statement in RETENTION_STATEMENTS.items()}


def due(now: datetime, last_run: datetime | None) -> bool:
	"""03:00 has passed today and it hasn't run since (a server that was off
	then catches up once when it's back)."""
	today = datetime.combine(now.date(), RUN_AT)
	return now >= today and (last_run is None or last_run < today)


def read_status() -> dict[str, Any] | None:
	"""{time, ok, counts | message} of the last run, or None (never ran)."""
	return runtime.read_json(runtime.config_dir() / STATUS_FILE)


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
		runtime.write_json(runtime.config_dir() / STATUS_FILE,
		                   {"time": stamp, "ok": False, "message": str(e).splitlines()[0]})
		raise
	status = {"time": stamp, "ok": True, "counts": counts}
	runtime.write_json(runtime.config_dir() / STATUS_FILE, status)
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


class NightlyCleanUp(runtime.PeriodicTask):
	"""The daily clean-up's loop: every CHECK_SECONDS (the first check after
	one), the clean-up when it's due and not held - the database looked up
	each time, so it follows a move. A failure is retried after RETRY."""
	FAILURE = "ACTION NEEDED - the nightly clean-up failed: {error} (tried again in an hour)"

	def __init__(self, backend: BackendServices, hold: Callable[[], bool]) -> None:
		super().__init__("retention", CHECK_SECONDS, first_delay=CHECK_SECONDS, hold=hold)
		self.backend = backend
		self.last_run = _last_success()      # a restart doesn't run it again
		self.retry_at: datetime | None = None
		self.now = datetime.min              # this turn's time

	def run_once(self) -> None:
		self.now = datetime.now()
		if (self.retry_at and self.now < self.retry_at) or not due(self.now, self.last_run) \
				or self.hold():
			return
		run_once(self.backend.postgres.engine, self.now)
		self.last_run, self.retry_at = self.now, None

	def failed(self, error: Exception) -> None:
		super().failed(error)
		self.retry_at = self.now + RETRY


def start_retention(backend: BackendServices,
                    hold: Callable[[], bool] = lambda: False) -> None:
	"""The daily clean-up, from a daemon thread (NightlyCleanUp). Called by
	the web app's entry point. Never raises. hold(): True while a database
	move runs - the clean-up waits for it."""
	NightlyCleanUp(backend, hold).start()
