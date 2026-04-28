import os
import redis

def _build_url() -> str:
    if os.environ.get("REDIS_URL"):
        return os.environ["REDIS_URL"]
    host     = os.environ.get("REDIS_HOST", "localhost")
    port     = os.environ.get("REDIS_PORT", "6379")
    db       = os.environ.get("REDIS_DB", "0")
    password = os.environ.get("REDIS_PASSWORD", "")
    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"

redis_client = redis.from_url(_build_url())
