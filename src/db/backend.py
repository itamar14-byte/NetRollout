from sqlalchemy.exc import OperationalError
from redis.exceptions import ConnectionError as RedisConnectionError


from src.db.db_install import install
from src.db.postgres_db import PostgresConnection
from src.db.redis_db import RedisConnection



class BackendServices:
	def __init__(self, pg: PostgresConnection, redis: RedisConnection):
		self.postgres = pg
		install(self.postgres)
		self.redis = redis

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

	def reload_postgres(self,config):
		self.postgres.reload_db(config)

	def reload_redis(self, config):
		self.redis.reload_db(config)

