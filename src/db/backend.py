from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from src.db.db_install import install
from src.db.postgres_db import PostgresConnection, PostgresConfig
from src.db.redis_db import RedisConnection, RedisConfig


class BackendServices:
	def __init__(self):
		#initilaize db reference files
		self._CONFIG_ENV = Path(__file__).parent.parent / "config.env"
		self._FLAG = Path(__file__).parent.parent / "pending_db_init.flag"
		#read config
		load_dotenv(self._CONFIG_ENV, override=True)
		#initialize db instances
		self.postgres = PostgresConnection()
		install(self.postgres)
		self.redis = RedisConnection()

	def health(self):
		try:
			postgres_up = self.postgres.test_connection()
		except OperationalError:
			postgres_up = False

		try:
			redis_up = self.redis.test_connection()
		except RedisConnectionError:
			redis_up = False
		return {
			"postgres": postgres_up,
			"redis": redis_up
		}

	def _write_config(self, updates: dict, pop_keys: list | None = None):
		cfg = dict(dotenv_values(self._CONFIG_ENV)) if \
			self._CONFIG_ENV.exists() else {}
		cfg.update(updates)
		for key in (pop_keys or []):
			cfg.pop(key, None)
		self._CONFIG_ENV.write_text(
			"\n".join(f"{k}={v}" for k, v in cfg.items()) + "\n"
		)

	def connection_modes(self):
		return {
			"POSTGRES": "bundled" if self.postgres.config.host in (
				"localhost", "127.0.0.1") else "external",
			"REDIS": "bundled" if self.redis.config.host in (
				"localhost", "127.0.0.1") else "external",
		}

	# TODO catch reload errors upstream in replacement route
	def reload_postgres(self, config: PostgresConfig):
		self.postgres.reload_db(config)
		updates, pop_keys = config.to_env_dict()
		self._write_config(updates, pop_keys)
		self._FLAG.write_text(".")

	def reload_redis(self, config: RedisConfig):
		self.redis.reload_db(config)
		updates, pop_keys = config.to_env_dict()
		self._write_config(updates, pop_keys)
