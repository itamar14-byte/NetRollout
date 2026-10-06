"""NetRollout's Redis connection: its settings (from config/runtime.env or the
environment) and one client the whole app shares - every user looks the
client up per call, so a Server Management switch takes effect at once."""
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
	"""Where Redis is: a URL, or host / port / database number / password."""
	host: str = "localhost"
	port: str = "6379"
	db: str = "0"
	password: str | None = None
	url: str | None = None      # wins over the parts when set

	@classmethod
	def unload_env(cls) -> "RedisConfig":
		"""The settings from the environment (REDIS_URL, else REDIS_HOST /
		_PORT / _DB / _PASSWORD), with the defaults for what's missing."""
		return cls(
			os.getenv("REDIS_HOST", "localhost"),
			os.getenv("REDIS_PORT", "6379"),
			os.getenv("REDIS_DB", "0"),
			os.getenv("REDIS_PASSWORD", ""),
			os.getenv("REDIS_URL")
		)

	def get_url(self) -> str:
		""":returns: the redis:// URL to connect with (the password in it)"""
		if self.url:
			return self.url
		if self.password:
			return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
		return f"redis://{self.host}:{self.port}/{self.db}"

	def place(self) -> tuple[str | None, int, int]:
		"""Which Redis: server, port, database number, whatever the password."""
		parts = parse_url(self.get_url())
		return (parts.get("host"), int(parts.get("port") or 6379), int(parts.get("db") or 0))

	def to_env_dict(self) -> dict[str, str]:
		"""The settings as config/runtime.env keys - every key, blank rather
		than absent: runtime.env is loaded over the container environment, and
		an inherited REDIS_URL or REDIS_PASSWORD would otherwise still apply
		after a switch."""
		return {
			"REDIS_HOST": self.host,
			"REDIS_PORT": self.port,
			"REDIS_DB": self.db or "0",
			"REDIS_PASSWORD": self.password or "",
			"REDIS_URL": "",
		}



class RedisConnection:
	"""The app's Redis client, replaceable live (reload_db)."""

	def __init__(self, config: RedisConfig | None = None):
		""":param config: where Redis is; the environment's settings when None"""
		self.config = config or RedisConfig.unload_env()
		self.client = self._build_client(self.config)

	@staticmethod
	def _build_client(config: RedisConfig) -> redis.Redis:
		"""A client with short timeouts and one retry, not connected yet."""
		# Without explicit timeouts an unreachable (silent) host costs the OS
		# connect timeout on every attempt, times redis-py's default retries
		# — ~20s per request. SOCKET_TIMEOUT must stay above the
		# dispatcher's BLPOP wait, which blocks by design.
		return redis.from_url(config.get_url(),
							  socket_connect_timeout=CONNECT_TIMEOUT,
							  socket_timeout=SOCKET_TIMEOUT,
							  retry=Retry(NoBackoff(), 1))

	def test_connection(self) -> bool:
		""":returns: True when Redis answers a PING
		:raises redis.exceptions.ConnectionError: refused
		:raises redis.exceptions.TimeoutError: no answer (see REDIS_UNAVAILABLE)"""
		self.client.ping()
		return True

	def reload_db(self, config: RedisConfig | None = None) -> None:
		"""Switch to another Redis live: the new client is checked first, then
		swapped in and the old one closed.

		:param config: the new Redis; the environment's settings when None
		:raises RuntimeError: the new server refused or didn't answer -
		 nothing changed"""
		new_config = config or RedisConfig.unload_env()
		new_client = self._build_client(new_config)

		try:
			new_client.ping()
		except REDIS_UNAVAILABLE:      # refused, or no answer (a TimeoutError)
			raise RuntimeError("New server unavailable")

		old_client = self.client
		self.config = new_config
		self.client = new_client

		old_client.close()

	def disconnect(self) -> None:
		"""Close the client's connections (it reconnects when used again)."""
		self.client.close()
