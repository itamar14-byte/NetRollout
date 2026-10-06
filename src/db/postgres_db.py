import os
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy import URL, create_engine, make_url, text

from src.db.db_install import install


@dataclass(frozen=True)
class PostgresConfig:
	host: str = "localhost"
	port: str = "5432"
	database: str = "rollout_db"
	user: str = "dbadmin"
	password: str = "Pass123"
	schema: str | None  = None
	url: str | None = None

	@classmethod
	def unload_env(cls):
		return cls (
			os.getenv("PG_HOST", "localhost"),
			os.getenv("PG_PORT", "5432"),
			os.getenv("PG_NAME", "rollout_db"),
			os.getenv("PG_USER", "dbadmin"),
			os.getenv("PG_PASSWORD", "Pass123"),
			os.getenv("PG_SCHEMA"),
			os.getenv("DATABASE_URL")
		)

	def get_url(self):
		if self.url:
			return self.url
		# URL.create escapes: a password with @ / : # stays one password
		return URL.create("postgresql+psycopg2", username=self.user,
		                  password=self.password, host=self.host,
		                  port=int(self.port), database=self.database
		                  ).render_as_string(hide_password=False)

	def place(self) -> tuple:
		"""Where the data is - server, port, database, schema - whatever the
		login: two configs with the same place are the same database."""
		url = make_url(self.get_url())
		return (url.host, url.port or 5432, url.database, self.schema or "public")

	def to_env_dict(self) -> dict:
		# Every key, blank rather than absent: runtime.env is loaded over the
		# container environment, and an inherited DATABASE_URL or PG_SCHEMA
		# would otherwise still apply after the switch
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
	def __init__(self, config: PostgresConfig | None = None):
		self.config = config or PostgresConfig.unload_env()
		self.engine = self._build_engine(self.config)
		self.session_factory = sessionmaker(bind=self.engine)

	@staticmethod
	def _build_engine(config):
		connect_args = {"options": f"-c search_path={config.schema}"} if (
			config.schema) else {}
		return create_engine(config.get_url(), connect_args=connect_args,
		                     pool_pre_ping=True)


	@contextmanager
	def get_session(self):
		session = self.session_factory()
		try:
			yield session
			session.commit()
		except Exception:
			session.rollback()
			raise
		finally:
			session.close()

	def test_connection(self):
		with self.get_session() as conn:
			conn.execute(text("SELECT 1"))
		return True

	def reload_db(self, config: PostgresConfig | None = None,
	              install_flag=True):
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

	def disconnect(self):
		self.engine.dispose()

