"""The inventory (src/inventory.py): ReachabilityChecker (probe, cache,
refresh, recheck-before-block) and the CSV import (import_csv)."""
import os
import socket
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

import redis

from src.inventory import ReachabilityChecker, import_csv, probe
from src.rollout.engine import Device
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger


class FakeRedis:
	"""An in-memory stand-in for the cache's Redis (mget, pipelined setex); down
	makes every call raise ConnectionError."""
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
	"""A prober that records every (ip, port) it is asked and answers reachable
	unless the target is in down."""
	def __init__(self, down=()):
		self.down, self.calls = set(down), []

	def __call__(self, ip, port):
		self.calls.append((ip, port))
		return (ip, port) not in self.down


A, B = ("10.0.0.1", 22), ("10.0.0.2", 2002)


def checker(fake=None, down=()):
	"""A ReachabilityChecker on a FakeRedis and a CountingProbe; returns
	(checker, probe, fake redis)."""
	fake = fake or FakeRedis()
	p = CountingProbe(down)
	return ReachabilityChecker(lambda: fake, prober=p), p, fake


def test_results_are_probed_then_cached():
	"""The first check probes each target (A reachable, B not, with checked_at);
	a second check is served from the cache, probing nothing more."""
	c, p, _ = checker(down=[B])
	first = c.check([A, B])
	assert (first[A]["reachable"], first[B]["reachable"]) == (True, False)
	assert "checked_at" in first[A]
	c.check([A, B])
	assert sorted(p.calls) == [A, B]  # second call served from cache


def test_refresh_bypasses_the_cache():
	"""check(refresh=True) probes again even though the result is cached."""
	c, p, _ = checker()
	c.check([A])
	c.check([A], refresh=True)
	assert p.calls == [A, A]


def test_recheck_unreachable_reprobes_only_cached_failures():
	"""recheck_unreachable trusts a cached reachable A and re-probes only the
	cached failure B, which is now reported reachable."""
	c, p, _ = checker(down=[B])
	c.check([A, B])
	p.down.clear()  # B came back
	result = c.check([A, B], recheck_unreachable=True)
	assert result[B]["reachable"] is True
	assert p.calls == [A, B, B]  # A trusted from cache, B re-probed


def test_redis_outage_falls_back_to_probing():
	"""With Redis down, check still answers by probing - every time, as nothing
	is cached."""
	c, p, _ = checker(fake=FakeRedis(down=True))
	assert c.check([A])[A]["reachable"] is True
	c.check([A])
	assert p.calls == [A, A]  # no cache, still answers


def test_ports_are_normalised():
	"""A port given as text ("22") is keyed as an int in the result."""
	c, p, _ = checker()
	assert set(c.check([("10.0.0.1", "22")])) == {A}


def test_probe_open_and_closed_local_ports():
	"""probe is True for a listening local port, False once the listener is gone,
	and False for an invalid address."""
	with socket.socket() as server:
		server.bind(("127.0.0.1", 0))
		server.listen()
		open_port = server.getsockname()[1]
		assert probe("127.0.0.1", open_port) is True
	# the port is closed again once the listener is gone
	assert probe("127.0.0.1", open_port, timeout=1) is False
	assert probe("not-an-address", 22, timeout=1) is False


# ── the CSV import (import_csv) ──────────────────────────────────────────────

class TestImportCsv(unittest.TestCase):

	def setUp(self):
		self.logger = RolloutLogger(webapp=False, verbose=False)
		self.validator = Validator(self.logger)
		self.parser = InputParser(self.validator, self.logger)
		self.db_session = MagicMock()
		self.user_id = uuid.uuid4()

	@staticmethod
	def _write_csv(path, rows):
		"""Write a devices CSV (ip, username, password, device_type, secret,
		port) with these rows."""
		with open(path, "w", encoding="utf-8") as f:
			f.write("ip,username,password,device_type,secret,port\n")
			for row in rows:
				f.write(",".join(str(row[k]) for k in
								 ("ip", "username", "password",
								  "device_type", "secret", "port")) + "\n")

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
	def test_import_csv_returns_devices(self, _):
		"""import_csv turns a one-row devices CSV into one Device."""
		with tempfile.TemporaryDirectory() as tmpdir:
			csv_path = os.path.join(tmpdir, "devices.csv")
			self._write_csv(csv_path, [
				{"ip": "10.0.0.1", "username": "admin", "password": "pass",
				 "device_type": "cisco_ios", "secret": "s", "port": "22"}
			])
			devices = import_csv(self.parser, csv_path, self.user_id, self.db_session).devices
		self.assertEqual(len(devices), 1)
		self.assertIsInstance(devices[0], Device)

	def test_import_csv_nonexistent_file_returns_empty(self):
		"""import_csv on a missing file gives no devices."""
		devices = import_csv(self.parser, "/no/such/file.csv", self.user_id, self.db_session).devices
		self.assertEqual(devices, [])

	def test_import_csv_wrong_extension_returns_empty(self):
		"""import_csv on a .txt file gives no devices."""
		with tempfile.TemporaryDirectory() as tmpdir:
			bad_path = os.path.join(tmpdir, "devices.txt")
			open(bad_path, "w").close()
			devices = import_csv(self.parser, bad_path, self.user_id, self.db_session).devices
		self.assertEqual(devices, [])

	def test_import_csv_missing_columns_returns_empty(self):
		"""import_csv on a CSV missing required columns gives no devices."""
		with tempfile.TemporaryDirectory() as tmpdir:
			csv_path = os.path.join(tmpdir, "devices.csv")
			with open(csv_path, "w") as f:
				f.write("ip,username\n10.0.0.1,admin\n")
			devices = import_csv(self.parser, csv_path, self.user_id, self.db_session).devices
		self.assertEqual(devices, [])
