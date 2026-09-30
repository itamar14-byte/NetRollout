import os
from functools import cached_property

from dotenv import dotenv_values, load_dotenv
from sqlalchemy.exc import OperationalError

from src import paths
from src.db.db_install import install
from src.db.postgres_db import PostgresConnection, PostgresConfig
from src.db.redis_db import RedisConnection, RedisConfig, REDIS_UNAVAILABLE
from src.db.settings import SettingsStore
from src.db.tables import SecurityProfile, LDAPServer, User

# Every column holding Fernet ciphertext
_ENCRYPTED_COLUMNS = (SecurityProfile.password_secret,
                      SecurityProfile.enable_secret,
                      LDAPServer.bind_password,
                      User.otp_secret)
# All Fernet tokens start with this (version byte 0x80, base64-encoded)
_FERNET_PREFIX = "gAAAAA"
# Hosts of the bundled services: local development, or the compose service
# names (deploy/compose.yaml must use these names)
BUNDLED_HOSTS = {"POSTGRES": ("localhost", "127.0.0.1", "postgres"),
                 "REDIS": ("localhost", "127.0.0.1", "redis")}


class BackendServices:
	def __init__(self):
		# config/runtime.env holds only what Server Management wrote (a
		# database / Redis switch); it wins over the container environment
		# (the installer's .env), which wins over the code defaults
		self._CONFIG_ENV = paths.runtime_env()
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
			for column in _ENCRYPTED_COLUMNS:
				value = db_session.query(column).filter(
					column.like(f"{_FERNET_PREFIX}%")).limit(1).scalar()
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

	def reload_redis(self, config: RedisConfig):
		self.redis.reload_db(config)
		self._write_config(config.to_env_dict())
