import os
import redis

redis_client = redis.from_url(
      os.environ.get("REDIS_URL", "redis://:RedisPass123@localhost:6379/0"))



