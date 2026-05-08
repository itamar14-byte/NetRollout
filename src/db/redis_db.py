import os
from dataclasses import dataclass

import redis

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

    def to_env_dict(self) -> tuple[dict, list]:
        updates = {
            "REDIS_HOST": self.host,
            "REDIS_PORT": self.port,
            "REDIS_DB": self.db or "0",
        }
        pop_keys = []
        if self.host in ("localhost", "127.0.0.1"):
            pop_keys.append("REDIS_EXTERNAL")
        else:
            updates["REDIS_EXTERNAL"] = "true"
        if self.password:
            updates["REDIS_PASSWORD"] = self.password
        else:
            pop_keys.append("REDIS_PASSWORD")
        return updates, pop_keys


    
class RedisConnection:
    def __init__(self, config: RedisConfig | None = None):
        self.config = config or RedisConfig.unload_env()
        self.client = self._build_client(self.config)
        
    @staticmethod
    def _build_client(config: RedisConfig) -> redis.Redis:
        return redis.from_url(config.get_url())

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

