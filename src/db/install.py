"""The database's set-up at every start (install: migrations, the factory
admin, the settings' rows) and what NetRollout's data needs around it:
Grafana's read access."""
import os
from typing import TYPE_CHECKING

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from src.db.settings import seed_settings
from src.db.tables import User, Role
if TYPE_CHECKING:
	from src.db.connections import PostgresConnection


ALEMBIC_INI = os.path.join(os.path.dirname(__file__), "alembic.ini")


def alembic_config(conn: Connection | None = None) -> AlembicConfig:
	"""Alembic's configuration for NetRollout's migrations, wherever it runs
	from (the script location made absolute).

	:param conn: the connection migrations run on (in its transaction);
	 None: Alembic's own (from alembic.ini)"""
	cfg = AlembicConfig(ALEMBIC_INI)
	cfg.set_main_option("script_location", os.path.join(os.path.dirname(ALEMBIC_INI), "alembic"))
	if conn is not None:
		cfg.attributes["connection"] = conn
	return cfg


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
				            role=Role.ADMIN,
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
