import os
from functools import cached_property

from dotenv import dotenv_values, load_dotenv
from sqlalchemy.exc import OperationalError

from src import runtime
from src.db.db_install import install, install_extras
from src.db.postgres_db import PostgresConnection, PostgresConfig
from src.db.redis_db import RedisConnection, RedisConfig, REDIS_UNAVAILABLE
from src.db.settings import SettingsStore
from src.db.tables import SecurityProfile, LDAPServer, User

# Every column holding Fernet ciphertext
ENCRYPTED_COLUMNS = (SecurityProfile.password_secret,
                      SecurityProfile.enable_secret,
                      LDAPServer.bind_password,
                      User.otp_secret)
# All Fernet tokens start with this (version byte 0x80, base64-encoded)
FERNET_PREFIX = "gAAAAA"
# Hosts of the bundled services: local development, or the compose service
# names (deploy/compose.yaml must use these names)
BUNDLED_HOSTS = {"POSTGRES": ("localhost", "127.0.0.1", "postgres"),
                 "REDIS": ("localhost", "127.0.0.1", "redis")}
# runtime.env: the bundled database's connection, kept when a move leaves it
# (the bundled Postgres keeps running, untouched) - Move back goes there
BUNDLED_DATABASE_KEY = "NETROLLOUT_BUNDLED_DATABASE_URL"


class BackendServices:
	def __init__(self):
		# config/runtime.env holds only what Server Management wrote (a
		# database / Redis switch); it wins over the container environment
		# (the installer's .env), which wins over the code defaults
		self._CONFIG_ENV = runtime.runtime_env()
		load_dotenv(self._CONFIG_ENV, override=True)
		#initialize db instances
		self.postgres = PostgresConnection()
		install(self.postgres)
		self.redis = RedisConnection()

	@cached_property
	def settings(self) -> SettingsStore:
		# the connection is looked up per call: follows a Server Management
		# database switch
		return SettingsStore(lambda: self.postgres)

	def health(self):
		try:
			postgres_up = self.postgres.test_connection()
		except OperationalError:
			postgres_up = False

		try:
			redis_up = self.redis.test_connection()
		except REDIS_UNAVAILABLE:
			redis_up = False
		return {
			"POSTGRES": postgres_up,
			"REDIS": redis_up
		}

	def encrypted_sample(self) -> str | None:
		"""One stored Fernet token from any encrypted column, or None if the
		DB holds no encrypted data. Only Fernet-shaped values are considered,
		so a legacy plaintext value can't fail the startup key check.
		:raises OperationalError: Postgres unreachable"""
		with self.postgres.get_session() as db_session:
			for column in ENCRYPTED_COLUMNS:
				value = db_session.query(column).filter(
					column.like(f"{FERNET_PREFIX}%")).limit(1).scalar()
				if value:
					return value
		return None

	def _write_config(self, updates: dict):
		cfg = dict(dotenv_values(self._CONFIG_ENV)) if \
			self._CONFIG_ENV.exists() else {}
		cfg.update(updates)
		# Atomic (a crash mid-write can't leave half a file) and owner-only:
		# it holds database / Redis passwords
		self._CONFIG_ENV.parent.mkdir(parents=True, exist_ok=True)
		tmp = self._CONFIG_ENV.with_name(self._CONFIG_ENV.name + ".tmp")
		tmp.write_text("\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n")
		os.chmod(tmp, 0o600)
		os.replace(tmp, self._CONFIG_ENV)

	def connection_modes(self):
		# The host of the live connection: with a DATABASE_URL / REDIS_URL,
		# config.host is only the default
		hosts = {
			"POSTGRES": self.postgres.engine.url.host,
			"REDIS": self.redis.client.connection_pool.connection_kwargs.get(
				"host"),
		}
		return {service: "bundled" if host in BUNDLED_HOSTS[service]
		        else "external" for service, host in hosts.items()}

	def reload_postgres(self, config: PostgresConfig):
		self.postgres.reload_db(config)
		self._write_config(config.to_env_dict())

	def bundled_postgres(self) -> PostgresConfig | None:
		"""The bundled database's connection: remembered by the move that left
		it, or the current one while on it; None if unknown (the database was
		switched by hand before moves existed)."""
		remembered = self._config_values().get(BUNDLED_DATABASE_KEY)
		if remembered:
			return PostgresConfig(url=remembered)
		if self.connection_modes()["POSTGRES"] == "bundled":
			return self.postgres.config
		return None

	def move_postgres(self, config: PostgresConfig) -> None:
		"""The switch at the end of a database move (src/webapp/db_move.py):
		the connection replaced live (pg_cron and Grafana's grants on the new
		one: install_extras), then runtime.env - leaving the bundled database,
		its connection is kept there first, for Move back.
		:raises RuntimeError: the new server doesn't answer (nothing changed)"""
		leaving = None
		if self.connection_modes()["POSTGRES"] == "bundled":
			leaving = self.postgres.engine.url.render_as_string(hide_password=False)
		# not install(): it would seed the factory admin into a copy whose
		# admins renamed or removed it
		self.postgres.reload_db(config, install_flag=False)
		install_extras(self.postgres)
		updates = config.to_env_dict()
		if config.url:          # PG_* blank: the URL is the connection
			updates.update({k: "" for k in updates}, DATABASE_URL=config.url)
		if leaving:
			updates[BUNDLED_DATABASE_KEY] = leaving
		self._write_config(updates)

	def _config_values(self) -> dict:
		return dict(dotenv_values(self._CONFIG_ENV)) if self._CONFIG_ENV.exists() else {}

	def reload_redis(self, config: RedisConfig):
		self.redis.reload_db(config)
		self._write_config(config.to_env_dict())
