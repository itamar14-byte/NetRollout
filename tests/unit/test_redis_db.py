"""RedisConnection.reload_db - a live Redis switch: refused in words when the
new server can't be reached, whichever way it fails, and nothing changes."""
import pytest
import redis

from src.db import redis_db
from src.db.redis_db import RedisConfig, RedisConnection


class FakeClient:
	"""Stands in for a redis.Redis: its PING fails as told, or answers."""
	def __init__(self, fails_with: Exception | None = None):
		self.fails_with, self.closed = fails_with, False

	def ping(self) -> bool:
		if self.fails_with:
			raise self.fails_with
		return True

	def close(self) -> None:
		self.closed = True


@pytest.mark.parametrize("failure", [
	redis.exceptions.ConnectionError("refused"),     # the server says no
	redis.exceptions.TimeoutError("no answer"),      # a silent host: not a ConnectionError
])
def test_an_unreachable_server_is_refused_in_words_and_nothing_changes(monkeypatch, failure):
	current = FakeClient()
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: current))
	conn = RedisConnection(RedisConfig(host="redis"))
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: FakeClient(failure)))

	with pytest.raises(RuntimeError, match="New server unavailable"):
		conn.reload_db(RedisConfig(host="cache.example.org"))
	assert conn.client is current and not current.closed
	assert conn.config.host == "redis"


def test_a_reachable_server_replaces_the_client(monkeypatch):
	old, new = FakeClient(), FakeClient()
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: old))
	conn = RedisConnection(RedisConfig(host="redis"))
	monkeypatch.setattr(RedisConnection, "_build_client", staticmethod(lambda config: new))
	conn.reload_db(RedisConfig(host="cache.example.org"))
	assert conn.client is new and old.closed and conn.config.host == "cache.example.org"


def test_both_failures_count_as_unavailable():
	assert redis.exceptions.TimeoutError in redis_db.REDIS_UNAVAILABLE
	assert redis.exceptions.ConnectionError in redis_db.REDIS_UNAVAILABLE
