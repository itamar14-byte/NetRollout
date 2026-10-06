"""NetRollout's PostgreSQL connection: its settings (from config/runtime.env
or the environment) and one engine + session factory the whole app shares -
replaceable live, by a database move."""
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import URL, create_engine, make_url, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from src.db.db_install import install


@dataclass(frozen=True)
class PostgresConfig:
	"""Where the database is: a URL, or host / port / database / login, and
	the schema NetRollout's tables live in (`public` when None)."""
	host: str = "localhost"
	port: str = "5432"
	database: str = "rollout_db"
	user: str = "dbadmin"
	password: str = "Pass123"
	schema: str | None  = None
	url: str | None = None      # wins over the parts when set

	@classmethod
	def unload_env(cls) -> "PostgresConfig":
		"""The settings from the environment (DATABASE_URL, else PG_HOST /
		_PORT / _NAME / _USER / _PASSWORD / _SCHEMA), with the defaults for
		what's missing."""
		return cls (
			os.getenv("PG_HOST", "localhost"),
			os.getenv("PG_PORT", "5432"),
			os.getenv("PG_NAME", "rollout_db"),
			os.getenv("PG_USER", "dbadmin"),
			os.getenv("PG_PASSWORD", "Pass123"),
			os.getenv("PG_SCHEMA"),
			os.getenv("DATABASE_URL")
		)

	def get_url(self) -> str:
		""":returns: the SQLAlchemy URL to connect with (the password in it)"""
		if self.url:
			return self.url
		# URL.create escapes: a password with @ / : # stays one password
		return URL.create("postgresql+psycopg2", username=self.user,
		                  password=self.password, host=self.host,
		                  port=int(self.port), database=self.database
		                  ).render_as_string(hide_password=False)

	def place(self) -> tuple[str | None, int, str | None, str]:
		"""Where the data is - server, port, database, schema - whatever the
		login: two configs with the same place are the same database."""
		url = make_url(self.get_url())
		return (url.host, url.port or 5432, url.database, self.schema or "public")

	def to_env_dict(self) -> dict[str, str]:
		"""The settings as config/runtime.env keys - every key, blank rather
		than absent: runtime.env is loaded over the container environment, and
		an inherited DATABASE_URL or PG_SCHEMA would otherwise still apply
		after a switch."""
		return {
			"PG_HOST": self.host,
			"PG_PORT": self.port,
			"PG_NAME": self.database,
			"PG_USER": self.user,
			"PG_PASSWORD": self.password,
			"PG_SCHEMA": self.schema or "",
			"DATABASE_URL": "",
		}

class PostgresConnection:
	"""The app's engine and sessions, replaceable live (reload_db)."""

	def __init__(self, config: PostgresConfig | None = None):
		""":param config: where the database is; the environment's settings
		 when None"""
		self.config = config or PostgresConfig.unload_env()
		self.engine = self._build_engine(self.config)
		self.session_factory = sessionmaker(bind=self.engine)

	@staticmethod
	def _build_engine(config: PostgresConfig) -> Engine:
		"""An engine for `config`, its schema first on the search path;
		connections are checked before use (pool_pre_ping) - a restarted
		server doesn't hand out dead ones."""
		connect_args = {"options": f"-c search_path={config.schema}"} if (
			config.schema) else {}
		return create_engine(config.get_url(), connect_args=connect_args,
		                     pool_pre_ping=True)


	@contextmanager
	def get_session(self) -> Iterator[Session]:
		"""A session for one unit of work: committed when the block ends,
		rolled back (and the error raised) when it fails, closed either way."""
		session = self.session_factory()
		try:
			yield session
			session.commit()
		except Exception:
			session.rollback()
			raise
		finally:
			session.close()

	def test_connection(self) -> bool:
		""":returns: True when the database answers
		:raises sqlalchemy.exc.OperationalError: it doesn't"""
		with self.get_session() as conn:
			conn.execute(text("SELECT 1"))
		return True

	def reload_db(self, config: PostgresConfig | None = None,
	              install_flag: bool = True) -> None:
		"""Switch to another database live: the new engine is checked first,
		then swapped in (sessions follow) and the old one disposed.

		:param config: the new database; the environment's settings when None
		:param install_flag: run install() on it (migrations, seeds); a
		 database move passes False - its copy brings everything
		:raises RuntimeError: the new server doesn't answer - nothing changed"""
		new_config = config or PostgresConfig.unload_env()
		new_engine = self._build_engine(new_config)

		try:
			with new_engine.connect() as conn:
				conn.execute(text("SELECT 1"))
		except OperationalError:
			raise RuntimeError("New server unavailable")

		old_engine = self.engine

		self.config = new_config
		self.engine = new_engine
		self.session_factory.configure(bind=new_engine)

		old_engine.dispose()

		if install_flag:
			install(self)

	def disconnect(self) -> None:
		"""Close the engine's pooled connections."""
		self.engine.dispose()
