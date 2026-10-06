import os
from dataclasses import dataclass

import redis
from redis.backoff import NoBackoff
from redis.connection import parse_url
from redis.retry import Retry

# Client timeouts (seconds): fail fast when Redis is unreachable
CONNECT_TIMEOUT = 3
SOCKET_TIMEOUT = 15  # > orchestration._BLPOP_TIMEOUT (enforced by a test)

# "Redis is unavailable": a refusing host raises ConnectionError, an
# unreachable one raises TimeoutError — which is NOT a ConnectionError
# subclass. Catch both wherever Redis being down should be survivable.
REDIS_UNAVAILABLE = (redis.exceptions.ConnectionError,
					 redis.exceptions.TimeoutError)

@dataclass(frozen=True)
class RedisConfig:
	host: str = "localhost"
	port: str = "6379"
	db: str = "0"
	password: str | None = None
	url: str | None = None

	@classmethod
	def unload_env(cls):
		return cls(
			os.getenv("REDIS_HOST", "localhost"),
			os.getenv("REDIS_PORT", "6379"),
			os.getenv("REDIS_DB", "0"),
			os.getenv("REDIS_PASSWORD", ""),
			os.getenv("REDIS_URL")
		)

	def get_url(self):
		if self.url:
			return self.url
		if self.password:
			return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
		return f"redis://{self.host}:{self.port}/{self.db}"

	def place(self) -> tuple:
		"""Which Redis: server, port, database number, whatever the password."""
		parts = parse_url(self.get_url())
		return (parts.get("host"), int(parts.get("port") or 6379), int(parts.get("db") or 0))

	def to_env_dict(self) -> dict:
		# Every key, blank rather than absent: runtime.env is loaded over the
		# container environment, and an inherited REDIS_URL or REDIS_PASSWORD
		# would otherwise still apply after the switch
		return {
			"REDIS_HOST": self.host,
			"REDIS_PORT": self.port,
			"REDIS_DB": self.db or "0",
			"REDIS_PASSWORD": self.password or "",
			"REDIS_URL": "",
		}


	
class RedisConnection:
	def __init__(self, config: RedisConfig | None = None):
		self.config = config or RedisConfig.unload_env()
		self.client = self._build_client(self.config)
		
	@staticmethod
	def _build_client(config: RedisConfig) -> redis.Redis:
		# Without explicit timeouts an unreachable (silent) host costs the OS
		# connect timeout on every attempt, times redis-py's default retries
		# — ~20s per request. SOCKET_TIMEOUT must stay above the
		# dispatcher's BLPOP wait, which blocks by design.
		return redis.from_url(config.get_url(),
							  socket_connect_timeout=CONNECT_TIMEOUT,
							  socket_timeout=SOCKET_TIMEOUT,
							  retry=Retry(NoBackoff(), 1))

	def test_connection(self):
		self.client.ping()
		return True

	
	def reload_db(self, config: RedisConfig | None = None):
		new_config = config or RedisConfig.unload_env()
		new_client = self._build_client(new_config)

		# validate connection before swapping
		try:
			new_client.ping()
		except redis.exceptions.ConnectionError:
			raise RuntimeError("New server unavailable")

		old_client = self.client
		self.config = new_config
		self.client = new_client

		old_client.close()

	def disconnect(self):
		self.client.close()

