"""Retention without pg_cron: many managed / an organisation's PostgreSQL
servers don't offer pg_cron, and then the nightly clean-up (job records,
config snapshots, the audit log - System Settings -> Retention) never ran.
The app runs the same statements itself, daily at 03:00 server time, when
pg_cron doesn't run them in the database it's connected to - checked at each
run, so a move to such a database is followed by itself."""
import threading
import time
from datetime import datetime, time as clock, timedelta

from src.db.db_install import pg_cron_runs_retention, run_retention

RUN_AT = clock(3, 0)          # as pg_cron's jobs ("0 3 * * *")
CHECK_SECONDS = 60
RETRY = timedelta(hours=1)


def due(now: datetime, last_run: datetime | None) -> bool:
	"""03:00 has passed today and it hasn't run since (a server that was off
	then catches up once when it's back)."""
	today = datetime.combine(now.date(), RUN_AT)
	return now >= today and (last_run is None or last_run < today)


def run_once(engine) -> dict | None:
	"""The clean-up now, unless pg_cron does it here; what each statement
	touched, or None (pg_cron's job)."""
	if pg_cron_runs_retention(engine):
		return None
	counts = run_retention(engine)
	print(f"[NetRollout] retention run by the app (no pg_cron in this database): "
	      + ", ".join(f"{name} {n}" for name, n in counts.items()), flush=True)
	return counts


def start_retention_fallback(backend) -> None:
	"""The daily check, from a daemon thread (the database looked up each
	time: it follows a move). Called by the web app's entry point. Never raises."""
	def loop():
		last_run = retry_at = None
		while True:
			time.sleep(CHECK_SECONDS)
			now = datetime.now()
			if (retry_at and now < retry_at) or not due(now, last_run):
				continue
			try:
				run_once(backend.postgres.engine)
				last_run, retry_at = now, None
			except Exception as e:          # noqa: BLE001 - keep the thread alive
				print(f"[NetRollout] ACTION NEEDED - the retention clean-up failed: {e} "
				      f"(tried again in an hour)", flush=True)
				retry_at = now + RETRY
	threading.Thread(target=loop, name="retention-fallback", daemon=True).start()
