import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy.engine import Connection

# make project root importable
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from src import runtime
from src.db.tables import Base
from src.db.postgres_db import PostgresConfig, PostgresConnection

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# When migrations run from the app (db_install.install), the app hands over
# its live connection: the exact database, credentials and schema it uses —
# including after a Server Management switch. The alembic CLI gets none and
# builds its own below.
shared_connection = config.attributes.get("connection")

# CLI only: inside the app, fileConfig would disable the app's own loggers
if shared_connection is None and config.config_file_name is not None:
	fileConfig(config.config_file_name)

target_metadata = Base.metadata


def cli_config() -> PostgresConfig:
	# Same resolution as the app: DATABASE_URL if set, else PG_* vars.
	# config/runtime.env is loaded WITHOUT override so variables set in the
	# shell (e.g. the test suite's DATABASE_URL) always win.
	load_dotenv(runtime.runtime_env(), override=False)
	return PostgresConfig.unload_env()


def run_migrations_offline() -> None:
	"""Run migrations in 'offline' mode: emit SQL for a URL, no connection."""
	context.configure(
		url=cli_config().get_url(),
		target_metadata=target_metadata,
		literal_binds=True,
		dialect_opts={"paramstyle": "named"},
	)

	with context.begin_transaction():
		context.run_migrations()


def _migrate(connection: Connection) -> None:
	"""Run the migrations on `connection`, in one transaction."""
	context.configure(connection=connection, target_metadata=target_metadata)
	with context.begin_transaction():
		context.run_migrations()


def run_migrations_online() -> None:
	"""Run migrations in 'online' mode, on the app's connection if given."""
	if shared_connection is not None:
		_migrate(shared_connection)
		return
	# CLI: build the engine the way the app does (incl. PG_SCHEMA search_path)
	engine = PostgresConnection._build_engine(cli_config())
	try:
		with engine.connect() as connection:
			_migrate(connection)
			connection.commit()
	finally:
		engine.dispose()


if context.is_offline_mode():
	run_migrations_offline()
else:
	run_migrations_online()
