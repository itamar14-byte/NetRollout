"""The app's Redis client must fail fast when Redis is unreachable, without
breaking the dispatcher's blocking BLPOP."""
import time

import pytest

import src.orchestration as orchestration
from src.db.redis_db import (CONNECT_TIMEOUT, REDIS_UNAVAILABLE, SOCKET_TIMEOUT,
                             RedisConfig, RedisConnection)

UNROUTABLE_HOST = "10.255.255.1"  # silently drops packets: a real timeout


def test_client_is_built_with_timeouts():
	"""The client's connections use CONNECT_TIMEOUT and SOCKET_TIMEOUT."""
	kwargs = RedisConnection(RedisConfig()).client.connection_pool.connection_kwargs
	assert kwargs["socket_connect_timeout"] == CONNECT_TIMEOUT
	assert kwargs["socket_timeout"] == SOCKET_TIMEOUT


def test_socket_timeout_exceeds_dispatcher_blpop_wait():
	"""SOCKET_TIMEOUT is longer than the dispatcher's BLPOP wait: the socket
	timeout applies to BLPOP too, and if it were shorter every idle dispatcher
	wait would raise instead of returning None."""
	assert SOCKET_TIMEOUT > orchestration._BLPOP_TIMEOUT


def test_unreachable_host_fails_fast():
	"""A PING to an unroutable host raises a REDIS_UNAVAILABLE error within
	2 x CONNECT_TIMEOUT + 2 seconds."""
	client = RedisConnection(RedisConfig(host=UNROUTABLE_HOST)).client
	start = time.monotonic()
	with pytest.raises(REDIS_UNAVAILABLE):
		client.ping()
	# one connect attempt + one retry, never the ~20s it used to take
	assert time.monotonic() - start < 2 * CONNECT_TIMEOUT + 2
