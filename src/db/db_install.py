import os
from typing import TYPE_CHECKING

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
	from src.db.postgres_db import PostgresConnection
from werkzeug.security import generate_password_hash
from src.db.tables import User

# ── Retention policy ─────────────────────────────────────────────────────────
# A job's commands (job_metadata) and its per-device results (device_results)
# are one logical record and expire together. The heavy per-device running
# config snapshot (device_results.fetched_config) is cleared much earlier —
# it only matters while investigating a failed verify.
JOB_RETENTION_DAYS = 30
CONFIG_SNAPSHOT_RETENTION_DAYS = 7
AUDIT_RETENTION_DAYS = 90

_RETENTION_JOBS = {
	"device_result_retention":
		f"DELETE FROM device_results "
		f"WHERE completed_at < NOW() - INTERVAL '{JOB_RETENTION_DAYS} days'",
	# created_at is submission time, results age from completion — only
	# delete metadata once the job's results are gone, so both expire together
	"job_metadata_retention":
		f"DELETE FROM job_metadata m "
		f"WHERE m.created_at < NOW() - INTERVAL '{JOB_RETENTION_DAYS} days' "
		f"AND NOT EXISTS (SELECT 1 FROM device_results r "
		f"WHERE r.job_id = m.job_id)",
	# clear the payload, keep the row (status/analytics survive)
	"device_result_config_retention":
		f"UPDATE device_results SET fetched_config = NULL "
		f"WHERE fetched_config IS NOT NULL AND completed_at < NOW() - "
		f"INTERVAL '{CONFIG_SNAPSHOT_RETENTION_DAYS} days'",
	"audit_log_retention":
		f"DELETE FROM audit_log "
		f"WHERE timestamp < NOW() - INTERVAL '{AUDIT_RETENTION_DAYS} days'",
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
	try:
		with postgres.engine.connect() as conn:
			conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_cron;"))
			for name, statement in _RETENTION_JOBS.items():
				_schedule_retention(conn, name, statement)
			conn.commit()

		# Update DB Schema to last Alembic revision
		alembic_cfg = AlembicConfig(os.path.join(os.path.dirname(__file__),
		                                         'alembic.ini'))
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
		print("DB Initialized")
	except SQLAlchemyError as e:
		print(f"Initialization Error: {e}")
