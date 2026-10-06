"""Moving NetRollout's data to another PostgreSQL server (stage 9.8) - the
database part, without the web app: check a target, prepare it (the SQL a
DBA runs, or done with an administrator login given once), copy the data.

The copy is the backup engine's (src/backup.py): a `before-move` backup of
the current database (one consistent snapshot - and a way back), restored
into the target through NetRollout's own migrations in one transaction (only
NetRollout's tables; anything else in that schema is never touched), the key
checked, then the row counts compared. The switch itself (the app's
connection) is the caller's: src/webapp/db_move.py.

"The target" is the database *and schema* the connection names (`public`
unless one is given); other schemas aren't looked at."""
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from src import backup
from src.db.db_install import GRAFANA_ROLE
from src.db.postgres_db import PostgresConfig

MIN_SERVER_VERSION = 130000          # gen_random_uuid() is core from 13
DEFAULT_DATABASE = "netrollout"
DEFAULT_LOGIN = "netrollout"
EMPTY, NETROLLOUT, CLASH = "empty", "netrollout", "clash"
CONNECT_SECONDS = 10


class MoveError(Exception):
	"""Refused or failed - in words for the page; nothing was switched."""


def new_password() -> str:
	return secrets.token_urlsafe(24)


def engine_for(config: PostgresConfig) -> Engine:
	"""An engine for a target - as the app would connect (its schema first
	on the search path), failing fast when the server doesn't answer."""
	connect_args = {"connect_timeout": CONNECT_SECONDS}
	if config.schema:
		connect_args["options"] = f"-c search_path={config.schema}"
	return create_engine(config.get_url(), connect_args=connect_args, pool_pre_ping=True)


# ── Checking a target ────────────────────────────────────────────────────────

@dataclass
class Report:
	"""What a target holds and whether NetRollout can move there."""
	problems: list[str] = field(default_factory=list)   # any -> refused
	notes: list[str] = field(default_factory=list)      # worth knowing
	server_version: str = ""
	schema: str = ""
	contents: str = ""             # empty / netrollout / clash
	others: list[str] = field(default_factory=list)     # other applications' tables
	pg_cron: bool = False
	grafana_reader: bool = False

	@property
	def ok(self) -> bool:
		return not self.problems

	def as_dict(self) -> dict:
		return {"ok": self.ok, "problems": self.problems, "notes": self.notes,
		        "server_version": self.server_version, "schema": self.schema,
		        "contents": self.contents, "others": self.others,
		        "pg_cron": self.pg_cron, "grafana_reader": self.grafana_reader}


def _our_names() -> set[str]:
	from src.db.tables import Base
	return {t.name for t in Base.metadata.sorted_tables}


def _known_revisions() -> set[str]:
	script = ScriptDirectory.from_config(backup._alembic_config())
	return {r.revision for r in script.walk_revisions()}


def check_target(config: PostgresConfig) -> Report:
	"""Reachable with this login, a recent enough server, a schema the login
	can create tables in, and nothing there that isn't NetRollout's under a
	NetRollout table's name."""
	report = Report()
	engine = engine_for(config)
	try:
		with engine.connect() as conn:
			version = int(conn.execute(text("SHOW server_version_num")).scalar())
			report.server_version = conn.execute(text("SHOW server_version")).scalar().split()[0]
			if version < MIN_SERVER_VERSION:
				report.problems.append(f"PostgreSQL {report.server_version} is too old - "
				                       f"NetRollout needs 13 or newer.")
			schema = conn.execute(text("SELECT current_schema()")).scalar()
			if not schema:
				# current_schema() skips a schema the login may not use, too
				wanted = config.schema or "public"
				exists = conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :s"),
				                      {"s": wanted}).scalar()
				report.problems.append(
					f"The login {config.user} can't create tables in schema \"{wanted}\" - "
					f"it needs to own it (see the preparation SQL)." if exists else
					f"The schema \"{wanted}\" doesn't exist in {config.database} - create "
					f"it (see the preparation SQL).")
				return report
			report.schema = schema
			if not conn.execute(text("SELECT has_schema_privilege(:s, 'CREATE')"), {"s": schema}).scalar():
				report.problems.append(f"The login {config.user} can't create tables in schema "
				                       f"\"{schema}\" - it needs to own it (see the preparation SQL).")
			tables = set(inspect(conn).get_table_names(schema=schema))
			ours = _our_names() & tables
			if "alembic_version" in tables:
				revision = conn.execute(text(f'SELECT version_num FROM "{schema}".alembic_version')).scalar()
				netrollout = revision in _known_revisions()
			else:
				netrollout = False
			if netrollout:
				report.contents = NETROLLOUT
				report.notes.append("It holds a NetRollout database already - it will be "
				                    "replaced by this one's data.")
			elif ours or "alembic_version" in tables:
				report.contents = CLASH
				clash = sorted(ours | ({"alembic_version"} & tables))
				report.problems.append(
					f"Schema \"{schema}\" holds tables of another application under names "
					f"NetRollout uses ({', '.join(clash)}) - use another schema or database.")
			else:
				report.contents = EMPTY
			report.others = sorted(tables - _our_names() - {"alembic_version"})
			if report.others and report.contents != CLASH:
				report.notes.append(f"Other tables there ({len(report.others)}) are left alone.")
			report.pg_cron = bool(conn.execute(text(
				"SELECT 1 FROM pg_extension WHERE extname = 'pg_cron'")).scalar())
			if not report.pg_cron:
				report.notes.append("pg_cron isn't installed there: NetRollout runs the nightly "
				                    "clean-up itself.")
			report.grafana_reader = bool(conn.execute(text(
				"SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": GRAFANA_ROLE}).scalar())
			if not report.grafana_reader:
				report.notes.append(f"There's no {GRAFANA_ROLE} login: Grafana's dashboards stay "
				                    f"empty until it's created (see the preparation SQL).")
	except SQLAlchemyError as e:
		report.problems.append(f"Couldn't connect: {_reason(e)}")
	finally:
		engine.dispose()
	return report


def _reason(e: Exception) -> str:
	text_ = str(getattr(e, "orig", None) or e).strip().splitlines()
	return text_[0] if text_ else type(e).__name__


# ── Preparing a target ───────────────────────────────────────────────────────

def _ident(name: str) -> str:
	return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
	return "'" + value.replace("'", "''") + "'"


@dataclass
class Plan:
	"""What to create on the target: names and the passwords to use."""
	database: str = DEFAULT_DATABASE
	schema: str = "public"
	login: str = DEFAULT_LOGIN
	password: str = field(default_factory=new_password)
	grafana_password: str | None = None      # Grafana's (GRAFANA_DB_PASSWORD), if known


# What the DBA is asked for, in words - shown above the SQL (9.8d)
ACCESS_NEEDED = (
	"A database for NetRollout (it may be shared with other applications, in "
	"a schema of its own).",
	"A login NetRollout uses every day, owning that schema: NetRollout creates "
	"and upgrades its own tables there. It needs no administrator rights - no "
	"superuser, no CREATEDB, no CREATEROLE.",
	f"A read-only login for Grafana's dashboards ({GRAFANA_ROLE}); NetRollout "
	"itself grants it SELECT on exactly three tables (job results, job "
	"metadata, the audit log) - never users, credentials or settings.",
	"Optional: pg_cron in that database, with usage granted to NetRollout's "
	"login, for the nightly clean-up - without it NetRollout runs it itself.",
	"Network access from this server to the database server's port.",
)


def preparation_sql(plan: Plan) -> str:
	"""The SQL a DBA runs as an administrator (psql), passwords filled in."""
	db, login, schema = _ident(plan.database), _ident(plan.login), plan.schema or "public"
	lines = [
		"-- NetRollout: run as a PostgreSQL administrator (for example in psql)",
		f"CREATE ROLE {login} LOGIN PASSWORD {_literal(plan.password)};",
	]
	if plan.grafana_password:
		lines.append(f"CREATE ROLE {GRAFANA_ROLE} LOGIN PASSWORD {_literal(plan.grafana_password)};")
	else:
		lines.append(f"-- CREATE ROLE {GRAFANA_ROLE} LOGIN PASSWORD '<Grafana's database password>';")
	lines += [f"CREATE DATABASE {db} OWNER {login};",
	          f"\\connect {db}"]
	if schema == "public":
		lines.append(f"ALTER SCHEMA public OWNER TO {login};")
	else:
		lines.append(f"CREATE SCHEMA {_ident(schema)} AUTHORIZATION {login};")
	lines += [f"GRANT USAGE ON SCHEMA {_ident(schema)} TO {GRAFANA_ROLE};",
	          "-- Optional, where pg_cron is installed in this database:",
	          f"-- GRANT USAGE ON SCHEMA cron TO {login};"]
	return "\n".join(lines) + "\n"


def prepare_with_admin(host: str, port: str, admin_user: str, admin_password: str,
                       plan: Plan) -> list[str]:
	"""The same as preparation_sql, done with an administrator login given
	once (never stored). Creates what's missing; refuses to take over what
	exists and isn't NetRollout's to change. Returns what was done.
	:raises MoveError"""
	done = []
	admin = PostgresConfig(host=host, port=port, database="postgres",
	                       user=admin_user, password=admin_password)
	engine = engine_for(admin).execution_options(isolation_level="AUTOCOMMIT")
	try:
		with engine.connect() as conn:
			if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": plan.login}).scalar():
				raise MoveError(f"A login named {plan.login} exists on that server already - "
				                f"pick another name, or have the DBA prepare it.")
			db_owner = conn.execute(text(
				"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :d"),
				{"d": plan.database}).scalar()
			if db_owner:
				raise MoveError(f"A database named {plan.database} exists there already "
				                f"(owned by {db_owner}) - pick another name.")
			conn.execute(text(f"CREATE ROLE {_ident(plan.login)} LOGIN PASSWORD {_literal(plan.password)}"))
			done.append(f"login {plan.login} created")
			has_grafana = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
			                           {"r": GRAFANA_ROLE}).scalar()
			if not has_grafana and plan.grafana_password:
				conn.execute(text(f"CREATE ROLE {GRAFANA_ROLE} LOGIN PASSWORD "
				                  f"{_literal(plan.grafana_password)}"))
				done.append(f"login {GRAFANA_ROLE} created")
				has_grafana = True
			elif has_grafana:
				done.append(f"login {GRAFANA_ROLE} exists - left as it is")
			conn.execute(text(f"CREATE DATABASE {_ident(plan.database)} OWNER {_ident(plan.login)}"))
			done.append(f"database {plan.database} created")
		inside = engine_for(PostgresConfig(host=host, port=port, database=plan.database,
		                                   user=admin_user, password=admin_password)
		                    ).execution_options(isolation_level="AUTOCOMMIT")
		try:
			with inside.connect() as conn:
				schema = plan.schema or "public"
				if schema == "public":
					conn.execute(text(f"ALTER SCHEMA public OWNER TO {_ident(plan.login)}"))
				else:
					conn.execute(text(f"CREATE SCHEMA {_ident(schema)} AUTHORIZATION {_ident(plan.login)}"))
					done.append(f"schema {schema} created")
				if has_grafana:
					conn.execute(text(f"GRANT USAGE ON SCHEMA {_ident(schema)} TO {GRAFANA_ROLE}"))
		finally:
			inside.dispose()
	except SQLAlchemyError as e:
		raise MoveError(f"Preparing the database failed: {_reason(e)}"
		                + (f" (done before that: {', '.join(done)})" if done else "")) from None
	finally:
		engine.dispose()
	return done


# ── Copying ──────────────────────────────────────────────────────────────────

@dataclass
class Copied:
	backup: Path
	tables: dict                   # table -> rows, as copied


def copy(source: Engine, target: PostgresConfig, *, detail: dict,
         places: backup.Places | None = None, report=lambda step: None) -> Copied:
	"""A `before-move` backup of the source, restored into the target (one
	transaction: the target is unchanged on any failure), the row counts
	compared. Nothing must write to the source meanwhile (maintenance).
	:raises MoveError"""
	report("Backing up this database")
	try:
		path = backup.create(source, "before-move", places)
	except backup.BackupError as e:
		raise MoveError(f"The backup before the move failed: {e}") from None
	manifest = backup.read_manifest(path)
	engine = engine_for(target)
	try:
		report(f"Copying {sum(manifest.tables.values())} rows to {target.host}")
		try:
			backup.restore_database(path, engine, audit=("database.moved", detail))
		except backup.BackupError as e:
			raise MoveError(str(e)) from None
		except SQLAlchemyError as e:
			raise MoveError(f"Copying failed: {_reason(e)} - nothing was changed.") from None
		report("Comparing row counts")
		with engine.connect() as conn:
			counted = {t: conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
			           for t in manifest.tables}
		expected = dict(manifest.tables)
		expected["audit_log"] = expected.get("audit_log", 0) + 1     # database.moved
		wrong = {t: (expected[t], counted[t]) for t in expected if counted.get(t) != expected[t]}
		if wrong:
			raise MoveError("The copy doesn't match: " + ", ".join(
				f"{t} {n} rows, {got} there" for t, (n, got) in wrong.items())
				+ " - NetRollout stays on this database.")
	finally:
		engine.dispose()
	return Copied(path, counted)
