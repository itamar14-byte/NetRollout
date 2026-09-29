"""ReachabilityChecker: probe, cache, refresh, recheck-before-block."""
import socket

import redis

from src.reachability import ReachabilityChecker, probe


class FakeRedis:
	def __init__(self, down=False):
		self.store, self.down = {}, down

	def _check(self):
		if self.down:
			raise redis.exceptions.ConnectionError("down")

	def mget(self, keys):
		self._check()
		return [self.store.get(k) for k in keys]

	def pipeline(self):
		self._check()
		fake = self

		class Pipe:
			def __init__(self):
				self.ops = []

			def setex(self, key, ttl, value):
				self.ops.append((key, value))

			def execute(self):
				fake.store.update(dict(self.ops))
		return Pipe()


class CountingProbe:
	def __init__(self, down=()):
		self.down, self.calls = set(down), []

	def __call__(self, ip, port):
		self.calls.append((ip, port))
		return (ip, port) not in self.down


A, B = ("10.0.0.1", 22), ("10.0.0.2", 2002)


def checker(fake=None, down=()):
	fake = fake or FakeRedis()
	p = CountingProbe(down)
	return ReachabilityChecker(lambda: fake, prober=p), p, fake


def test_results_are_probed_then_cached():
	c, p, _ = checker(down=[B])
	first = c.check([A, B])
	assert (first[A]["reachable"], first[B]["reachable"]) == (True, False)
	assert "checked_at" in first[A]
	c.check([A, B])
	assert sorted(p.calls) == [A, B]  # second call served from cache


def test_refresh_bypasses_the_cache():
	c, p, _ = checker()
	c.check([A])
	c.check([A], refresh=True)
	assert p.calls == [A, A]


def test_recheck_unreachable_reprobes_only_cached_failures():
	c, p, _ = checker(down=[B])
	c.check([A, B])
	p.down.clear()  # B came back
	result = c.check([A, B], recheck_unreachable=True)
	assert result[B]["reachable"] is True
	assert p.calls == [A, B, B]  # A trusted from cache, B re-probed


def test_redis_outage_falls_back_to_probing():
	c, p, _ = checker(fake=FakeRedis(down=True))
	assert c.check([A])[A]["reachable"] is True
	c.check([A])
	assert p.calls == [A, A]  # no cache, still answers


def test_ports_are_normalised():
	c, p, _ = checker()
	assert set(c.check([("10.0.0.1", "22")])) == {A}


def test_probe_open_and_closed_local_ports():
	with socket.socket() as server:
		server.bind(("127.0.0.1", 0))
		server.listen()
		open_port = server.getsockname()[1]
		assert probe("127.0.0.1", open_port) is True
	# the port is closed again once the listener is gone
	assert probe("127.0.0.1", open_port, timeout=1) is False
	assert probe("not-an-address", 22, timeout=1) is False
