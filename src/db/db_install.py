import os
from typing import TYPE_CHECKING

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
	from src.db.postgres_db import PostgresConnection
from werkzeug.security import generate_password_hash
from src.db.settings import seed_settings, sql_value
from src.db.tables import User

# ── Retention policy ─────────────────────────────────────────────────────────
# A job's commands (job_metadata) and its per-device results (device_results)
# are one logical record and expire together. The heavy per-device running
# config snapshot (device_results.fetched_config) is cleared much earlier —
# it only matters while investigating a failed verify.
# The periods are System Settings (src/db/settings.py): each statement reads
# the setting's row when it runs, so a change applies at the next nightly
# run, without a restart.


def _older_than(column: str, setting: str) -> str:
	return f"{column} < NOW() - make_interval(days => {sql_value(setting)})"


_RETENTION_JOBS = {
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


def _schedule_retention(conn, name: str, statement: str):
	# Idempotent: unschedule-then-schedule, so every startup applies the
	# current policy to existing installs
	conn.execute(text(f"""
        DO $$
        BEGIN
            PERFORM cron.unschedule('{name}');
        EXCEPTION WHEN OTHERS THEN NULL;
        END;
        $$;
        SELECT cron.schedule('{name}', '0 3 * * *', $q${statement}$q$);
    """))


def install(postgres: "PostgresConnection"):
	# 1. Schema (essential). Migrations run on the app's own connection, so
	#    they hit the exact database/credentials/schema the app uses — also
	#    after a Server Management switch, and without DATABASE_URL.
	try:
		alembic_cfg = AlembicConfig(os.path.join(os.path.dirname(__file__),
		                                         'alembic.ini'))
		with postgres.engine.begin() as conn:
			alembic_cfg.attributes["connection"] = conn
			alembic_command.upgrade(alembic_cfg, "head")

		with postgres.get_session() as session:
			if not session.query(User).filter_by(username="admin").first():
				user = User(username="admin",
				            password_hash=generate_password_hash("admin"),
				            email="example@test.com",
				            full_name="Net Rollout",
				            role="admin",
				            is_active=True,
				            is_approved=True)
				session.add(user)
				session.flush()

		# every setting gets a row (install value or default); existing rows
		# are never changed — admins change them in System Settings
		with postgres.get_session() as session:
			for problem in seed_settings(session):
				print(f"[NetRollout] {problem}", flush=True)
	except SQLAlchemyError as e:
		print(f"Initialization Error: {e}")
		return

	# 2. Retention jobs (optional). pg_cron exists only where it's installed
	#    and configured (cron.database_name) — e.g. not on many external /
	#    managed Postgres servers. Its absence must never block the schema.
	try:
		with postgres.engine.connect() as conn:
			conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_cron;"))
			for name, statement in _RETENTION_JOBS.items():
				_schedule_retention(conn, name, statement)
			conn.commit()
	except SQLAlchemyError as e:
		print(f"[NetRollout] Retention jobs not scheduled — pg_cron "
		      f"unavailable in this database: {str(e).splitlines()[0]}")
	print("DB Initialized")
