"""The database's set-up at every start (install: migrations, the factory
admin, the settings' rows) and what NetRollout's data needs around it: the
nightly clean-up's statements, Grafana's read access."""
import os
from typing import TYPE_CHECKING

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from src.db.settings import seed_settings, sql_value
from src.db.tables import User
if TYPE_CHECKING:
	from src.db.connections import PostgresConnection


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
	"""The retention statements, once, in one transaction (the app runs them
	daily: src/db/retention.py). Each reads its period from
	system_settings.

	:param engine: the database NetRollout uses
	:returns: the rows each statement touched, by its name
	:raises sqlalchemy.exc.SQLAlchemyError: the database failed - nothing changed"""
	with engine.begin() as conn:
		return {name: conn.execute(text(statement)).rowcount
		        for name, statement in RETENTION_STATEMENTS.items()}


# ── Grafana's read access ────────────────────────────────────────────────────
# The dashboards read only these; users, credentials (security profiles),
# inventory, LDAP and settings stay out of reach. A new dashboard table is a
# deliberate addition here.
GRAFANA_ROLE = "grafana_reader"
GRAFANA_TABLES = ("device_results", "job_metadata", "audit_log")


def _grant_grafana_read(conn: Connection, role: str = GRAFANA_ROLE) -> bool:
	"""Exactly GRAFANA_TABLES readable by `role`, in the app's schema —
	repeated at every start (a table a migration recreates keeps it). Only
	where the role exists (the bundled Postgres creates it).

	:param conn: the app's own connection (the tables' owner may grant)
	:param role: Grafana's read-only login
	:returns: whether it was applied (False: no such login here)"""
	if not conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
	                    {"r": role}).scalar():
		return False
	schema = conn.execute(text("SELECT current_schema()")).scalar()
	conn.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" '
	                  f'FROM "{role}"'))
	conn.execute(text(f"GRANT SELECT ON {', '.join(GRAFANA_TABLES)} "
	                  f'TO "{role}"'))
	return True


def install(postgres: "PostgresConnection") -> None:
	"""Bring the database up to this version, at every start: the migrations,
	the factory admin when missing, a row for every setting, then
	install_extras. Idempotent. A database error is printed, not raised - the
	app starts and its pages say what's down.

	:param postgres: the app's connection - migrations run on it, so they hit
	 the exact database, login and schema the app uses (after a database move
	 too, and without DATABASE_URL)"""
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
				            is_approved=True,
				            must_change_password=True)
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
	install_extras(postgres)
	print("DB Initialized")


def install_extras(postgres: "PostgresConnection") -> None:
	"""What a database holding NetRollout's data needs beyond the data:
	Grafana's read access - at every start, and after a database move (whose
	copy brings everything else; nothing is seeded then)."""
	# Grafana's read access (optional): only where its role exists
	try:
		with postgres.engine.connect() as conn:
			_grant_grafana_read(conn)
			conn.commit()
	except SQLAlchemyError as e:
		print(f"[NetRollout] Grafana's read access not granted: "
		      f"{str(e).splitlines()[0]}")
