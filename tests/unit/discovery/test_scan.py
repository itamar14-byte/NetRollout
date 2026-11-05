"""One discovery (src/discovery/scan.py) with a fake port check, sign-in and
clock: what becomes a card and what a line, the addresses never signed in
to, the pause after failed sign-ins and Continue, Cancel, at most SIGN_INS
at once and SIGN_IN_GAP apart, the events since N, a finding created once,
and a scan that always ends. No sleeps decide anything: blocked sign-ins
wait on events, and waits are bounded."""
import socket
import threading
import time

import netmiko
import pytest

from src.discovery import scan as scan_module
from src.discovery.scan import (FAILURES_BEFORE_PAUSE, SIGN_IN_GAP, SIGN_INS, DiscoveryScan, Login, Phase,
                                Reason, Skip, read_banner)
from src.discovery.targets import parse_targets
from src.rollout.detect import Detection
from src.rollout.host_keys import HostKeyPolicy, HostKeyRefused, fingerprint


BANNER = "SSH-2.0-Cisco-1.25"
WAIT = 10   # seconds any one wait may take, at most


class Clock:
	"""The pacing's clock: it moves only when the scan sleeps."""

	def __init__(self):
		self.now = 0.0
		self._lock = threading.Lock()

	def __call__(self):
		with self._lock:
			return self.now

	def sleep(self, seconds):
		with self._lock:
			self.now += seconds


class Devices:
	"""The fake sign-in: each address's answer (a Detection or an exception
	to raise), optionally held until its gate opens; the calls, when they
	started (the clock) and how many ran at once."""

	def __init__(self, answers, clock, gates=None):
		self.answers, self.clock, self.gates = answers, clock, gates or {}
		self.calls, self.starts = [], []
		self.running = self.most = 0
		self._lock = threading.Lock()

	def __call__(self, device):
		with self._lock:
			self.calls.append(device)
			self.starts.append(self.clock())
			self.running += 1
			self.most = max(self.most, self.running)
		try:
			if gate := self.gates.get(device.ip):
				assert gate.wait(WAIT)
			answer = self.answers[device.ip]
			if isinstance(answer, Exception):
				raise answer
			return answer
		finally:
			with self._lock:
				self.running -= 1


def until(condition):
	"""Wait (bounded) for something the scan's threads do."""
	deadline = time.monotonic() + WAIT
	while not condition():
		assert time.monotonic() < deadline, "the scan didn't get there"
		time.sleep(0.005)


def make(text, answers, *, closed=(), skip=None, gates=None, skip_gates=None, on_end=None):
	""":param skip: endpoint -> Skip; skip_gates: address -> an event its
	 skip check (before its sign-in) waits for"""
	clock = Clock()
	devices = Devices(answers, clock, gates)

	def skipped(target):
		if gate := (skip_gates or {}).get(target.rsplit(":", 1)[0]):
			assert gate.wait(WAIT)
		return (skip or {}).get(target)

	found = DiscoveryScan(parse_targets(text, 4096), 22, Login("netops", "pw", "en"),
	                      skip=skipped,
	                      knock=lambda address, port: None if address in closed else BANNER,
	                      detect=devices, clock=clock, sleep=clock.sleep, on_end=on_end)
	return found, devices


def ended(found):
	found.start()
	assert found.wait(WAIT)
	return found


def lines(found):
	return [(e["address"], e["reason"], e["detail"]) for e in found.status()["events"] if e["kind"] == "line"]


XE = Detection(platform="cisco_xe", detected="cisco_xe", hostname="core-sw1",
               evidence="Cisco IOS XE Software, Version 17.09.04a")
AUTH = netmiko.exceptions.NetmikoAuthenticationException("Authentication failed.")


def test_each_answer_becomes_a_card_or_a_line():
	"""A supported platform and an unknown device are cards (the unknown one
	without a platform, for the admin to pick); a neighbour NetRollout
	doesn't support, wrong credentials, a timeout and any other error are
	lines with their reason; a closed port is neither."""
	found, _ = make("10.0.0.1-7", {
		"10.0.0.1": XE,
		"10.0.0.2": Detection(hostname="BR-SW-07"),
		"10.0.0.3": Detection(detected="cisco_asa", evidence="Cisco Adaptive Security Appliance Software"),
		"10.0.0.4": AUTH,
		"10.0.0.5": netmiko.exceptions.NetmikoTimeoutException("TCP connection to device failed."),
		"10.0.0.6": RuntimeError("Error reading SSH protocol banner\nmore"),
	}, closed={"10.0.0.7"})
	ended(found)
	cards = {f.address: (f.platform, f.hostname, f.banner) for f in found.findings()}
	assert cards == {"10.0.0.1": ("cisco_xe", "core-sw1", BANNER), "10.0.0.2": (None, "BR-SW-07", BANNER)}
	assert sorted(lines(found)) == [
		("10.0.0.3", "not-supported", "Cisco ASA"),
		("10.0.0.4", "wrong-credentials", ""),
		("10.0.0.5", "timed-out", ""),
		("10.0.0.6", "failed", "Error reading SSH protocol banner"),
	]
	counts = found.counts()
	assert (counts["addresses"], counts["checked"], counts["open"], counts["signed_in"], counts["found"],
	        counts["signing_in"]) == (7, 7, 6, 3, 2, 0)
	assert found.phase == Phase.DONE


def test_the_sign_in_uses_the_profile_and_the_port():
	"""The device signed in to is the address, the scan's port and the
	profile's credentials; no platform is assumed."""
	found, devices = make("10.0.0.1", {"10.0.0.1": XE})
	ended(found)
	(device,) = devices.calls
	assert (device.ip, device.port, device.username, device.password, device.secret, device.device_type) == \
		("10.0.0.1", 22, "netops", "pw", "en", "")


def test_skipped_addresses_are_never_signed_in_to():
	"""Already in Inventory or in a rollout (the skip rule, asked by
	endpoint): a line with the reason and its detail, no sign-in."""
	found, devices = make("10.0.0.1-3, 2001:db8::5", {"10.0.0.2": XE}, skip={
		"10.0.0.1:22": Skip(Reason.IN_INVENTORY, "edge-r1"),
		"10.0.0.3:22": Skip(Reason.IN_ROLLOUT, "dana's rollout"),
		"[2001:db8::5]:22": Skip(Reason.IN_INVENTORY, "v6-core"),
	})
	ended(found)
	assert [d.ip for d in devices.calls] == ["10.0.0.2"]
	assert sorted(lines(found)) == [("10.0.0.1", "in-inventory", "edge-r1"),
	                                ("10.0.0.3", "in-rollout", "dana's rollout"),
	                                ("2001:db8::5", "in-inventory", "v6-core")]


def test_a_failing_skip_rule_is_a_line_not_a_sign_in():
	"""If the skip rule can't answer, the address isn't signed in to."""
	found = DiscoveryScan(parse_targets("10.0.0.1", 4096), 22, Login("u", "p", ""),
	                      skip=lambda target: (_ for _ in ()).throw(RuntimeError("database down")),
	                      knock=lambda a, p: BANNER, detect=lambda d: pytest.fail("signed in"),
	                      sleep=lambda s: None)
	ended(found)
	assert lines(found) == [("10.0.0.1", "failed", "database down")]


def test_the_scan_pauses_after_the_failures_and_continue_resumes_it():
	"""Every sign-in fails: the scan pauses once FAILURES_BEFORE_PAUSE have
	failed, starts nothing more while paused, and Continue resumes it until
	the next FAILURES_BEFORE_PAUSE; the rest end it."""
	count = 3 * FAILURES_BEFORE_PAUSE
	found, devices = make(f"10.0.0.1-{count}", {f"10.0.0.{n}": AUTH for n in range(1, count + 1)})
	found.start()
	until(lambda: found.phase == Phase.PAUSED and found.counts()["signing_in"] == 0)
	first = len(devices.calls)
	assert FAILURES_BEFORE_PAUSE <= first < count
	assert len(devices.calls) == first                       # nothing started while paused
	assert found.proceed()
	until(lambda: found.phase in (Phase.PAUSED, Phase.DONE) and found.counts()["signing_in"] == 0)
	if found.phase == Phase.PAUSED:
		assert found.proceed()
	assert found.wait(WAIT)
	assert found.phase == Phase.DONE
	assert len(devices.calls) == count
	assert found.continues >= 1


def test_sign_ins_started_before_continue_dont_count_after_it():
	"""Paused at the third failure with a fourth sign-in still under way:
	after Continue that one failing, and two new failures, are only two
	since the Continue - the scan doesn't pause again."""
	addresses = [f"10.0.0.{n}" for n in range(1, 7)]
	first_three, fourth, later = threading.Event(), threading.Event(), threading.Event()
	gates = {a: first_three for a in addresses[:3]} | {addresses[3]: fourth}
	found, devices = make("10.0.0.1-6", {a: AUTH for a in addresses}, gates=gates,
	                      skip_gates={a: later for a in addresses[4:]})
	found.start()
	until(lambda: found.counts()["signing_in"] == SIGN_INS)
	first_three.set()
	until(lambda: found.phase == Phase.PAUSED and found.counts()["signing_in"] == 1)
	assert found.proceed()
	fourth.set()
	until(lambda: found.counts()["signing_in"] == 0)
	later.set()                                              # the last two start after the Continue
	assert found.wait(WAIT)
	phases = [e["phase"] for e in found.status()["events"] if e["kind"] == "phase"]
	assert phases == ["paused", "running", "done"]
	assert found.continues == 1
	assert len(devices.calls) == 6


def test_cancel_lets_the_sign_ins_under_way_finish_and_asks_no_more():
	"""Cancelled with SIGN_INS under way: they finish (their cards kept),
	the rest are "not asked", and it ends cancelled, by whom."""
	gate = threading.Event()
	addresses = [f"10.0.0.{n}" for n in range(1, 11)]
	found, devices = make("10.0.0.1-10", {a: XE for a in addresses}, gates={a: gate for a in addresses})
	found.start()
	until(lambda: found.counts()["signing_in"] == SIGN_INS)
	assert found.cancel("dana")
	assert found.phase == Phase.CANCELLING
	gate.set()
	assert found.wait(WAIT)
	assert (found.phase, found.cancelled_by) == (Phase.CANCELLED, "dana")
	assert len(devices.calls) == SIGN_INS
	assert len(found.findings()) == SIGN_INS
	assert found.counts()["reasons"]["not-asked"] == 10 - SIGN_INS
	assert not found.cancel("someone")                       # once


def test_cancel_while_paused_ends_it():
	"""Paused, then cancelled: nothing more is signed in to."""
	count = FAILURES_BEFORE_PAUSE + 5
	found, devices = make(f"10.0.0.1-{count}", {f"10.0.0.{n}": AUTH for n in range(1, count + 1)})
	found.start()
	until(lambda: found.phase == Phase.PAUSED and found.counts()["signing_in"] == 0)
	calls = len(devices.calls)
	assert found.cancel("restart")
	assert found.wait(WAIT)
	assert found.phase == Phase.CANCELLED
	assert len(devices.calls) == calls
	assert found.counts()["reasons"]["not-asked"] == count - calls
	assert not found.proceed()


def test_the_scan_stops_when_netrollout_says_so():
	"""stop_when answering a reason (a restart, a database move) before a
	sign-in starts cancels the scan by that reason: nothing more is signed
	in to."""
	stop = []
	found = DiscoveryScan(parse_targets("10.0.0.1-6", 4096), 22, Login("u", "p", ""), skip=lambda t: None,
	                      knock=lambda a, p: BANNER, detect=lambda d: (stop.append("restart"), XE)[1],
	                      sleep=lambda s: None, stop_when=lambda: stop[0] if stop else None)
	ended(found)
	assert (found.phase, found.cancelled_by) == (Phase.CANCELLED, "restart")
	assert len(found.findings()) <= SIGN_INS
	counts = found.counts()                     # every open port: a card or "not asked"
	assert counts["reasons"]["not-asked"] == counts["open"] - len(found.findings())


def test_at_most_sign_ins_at_once():
	"""With every sign-in held, exactly SIGN_INS start; none more until
	they end; never more than SIGN_INS at once."""
	gate = threading.Event()
	addresses = [f"10.0.0.{n}" for n in range(1, 13)]
	found, devices = make("10.0.0.1-12", {a: XE for a in addresses}, gates={a: gate for a in addresses})
	found.start()
	until(lambda: found.counts()["signing_in"] == SIGN_INS)
	assert len(devices.calls) == SIGN_INS
	gate.set()
	assert found.wait(WAIT)
	assert (devices.most, len(devices.calls)) == (SIGN_INS, 12)


def test_sign_ins_start_at_least_the_gap_apart():
	"""Each sign-in starts SIGN_IN_GAP after the one before, by the
	pacing's clock."""
	addresses = [f"10.0.0.{n}" for n in range(1, 9)]
	found, devices = make("10.0.0.1-8", {a: XE for a in addresses})
	ended(found)
	starts = sorted(devices.starts)
	assert len(starts) == 8
	assert all(later - earlier >= SIGN_IN_GAP - 1e-9 for earlier, later in zip(starts, starts[1:]))


def test_sign_ins_go_in_address_order(monkeypatch):
	"""With one sign-in at a time and every port answered while the first
	waits, the addresses are signed in to lowest first, whatever order the
	port checks ended in."""
	monkeypatch.setattr(scan_module, "SIGN_INS", 1)
	gate = threading.Event()
	addresses = [f"10.0.0.{n}" for n in range(1, 9)]
	found, devices = make("10.0.0.1-8", {a: XE for a in addresses}, skip_gates={addresses[0]: gate})
	found.start()
	until(lambda: found.counts()["checked"] == 8)
	gate.set()
	assert found.wait(WAIT)
	assert [d.ip for d in devices.calls] == addresses


def test_events_are_numbered_and_asked_for_since_n():
	"""Sequence numbers from 1 with no gap; status(since=N) gives only the
	events after N, and the last number."""
	found, _ = make("10.0.0.1-3", {"10.0.0.1": XE, "10.0.0.2": AUTH}, closed={"10.0.0.3"})
	ended(found)
	whole = found.status()
	assert [e["seq"] for e in whole["events"]] == list(range(1, whole["seq"] + 1))
	assert found.status(since=2)["events"] == whole["events"][2:]
	assert found.status(since=whole["seq"])["events"] == []
	kinds = [e["kind"] for e in whole["events"]]
	assert kinds.count("port") == 3 and kinds.count("finding") == 1 and kinds.count("line") == 1
	assert whole["events"][-1] == {"seq": whole["seq"], "kind": "phase", "phase": "done"}


def test_no_port_events_above_the_map_limit(monkeypatch):
	"""Above the map's size the page shows counters only: no event per
	address."""
	monkeypatch.setattr(scan_module, "MAP_LIMIT", 2)
	found, _ = make("10.0.0.1-3", {}, closed={"10.0.0.1", "10.0.0.2", "10.0.0.3"})
	ended(found)
	assert [e["kind"] for e in found.status()["events"]] == ["phase"]
	assert found.counts()["checked"] == 3


def test_a_finding_is_claimed_once():
	"""claim() hands a finding out once (a second click, another admin);
	release() lets it be tried again; an unknown one is None."""
	found, _ = make("10.0.0.1", {"10.0.0.1": XE})
	ended(found)
	(finding,) = found.findings()
	assert found.claim(finding.id) is finding
	assert found.claim(finding.id) is None
	found.release(finding.id)
	assert found.claim(finding.id) is finding
	assert found.claim(99) is None


def test_the_scan_always_ends_and_reports_once():
	"""A sign-in raising anything, a port check raising: the scan still
	ends DONE, on_end called once."""
	ends = []
	found = DiscoveryScan(parse_targets("10.0.0.1-2", 4096), 22, Login("u", "p", ""), skip=lambda t: None,
	                      knock=lambda a, p: (_ for _ in ()).throw(OSError("boom")) if a == "10.0.0.2" else BANNER,
	                      detect=lambda d: (_ for _ in ()).throw(KeyError("odd")),
	                      sleep=lambda s: None, on_end=ends.append)
	ended(found)
	assert found.phase == Phase.DONE
	assert ends == [found]
	assert lines(found) == [("10.0.0.1", "failed", "'odd'")]
	assert found.counts()["open"] == 1


def test_a_failing_on_end_doesnt_stop_the_end():
	"""on_end raising (the audit down) still leaves the scan ended."""
	found, _ = make("10.0.0.1", {"10.0.0.1": XE}, on_end=lambda s: (_ for _ in ()).throw(RuntimeError("db")))
	ended(found)
	assert found.phase == Phase.DONE


@pytest.fixture
def server():
	"""A local TCP server: what it sends on connect, set per test."""
	listener = socket.create_server(("127.0.0.1", 0))
	sent = {"data": b""}

	def serve():
		while True:
			try:
				conn, _ = listener.accept()
			except OSError:
				return
			with conn:
				if sent["data"]:
					conn.sendall(sent["data"])
				conn.recv(1)

	threading.Thread(target=serve, daemon=True).start()
	yield listener.getsockname()[1], sent
	listener.close()


def test_read_banner_returns_the_first_line(server):
	"""An SSH server's version line, without its CR LF."""
	port, sent = server
	sent["data"] = b"SSH-2.0-OpenSSH_9.2p1 Debian-2\r\nmore\r\n"
	assert read_banner("127.0.0.1", port) == "SSH-2.0-OpenSSH_9.2p1 Debian-2"


def test_read_banner_of_a_silent_port_is_empty(server, monkeypatch):
	"""Open but saying nothing within BANNER_TIMEOUT: "" (open)."""
	monkeypatch.setattr(scan_module, "BANNER_TIMEOUT", 0.2)
	port, _ = server
	assert read_banner("127.0.0.1", port) == ""


def test_read_banner_of_a_closed_port_is_none():
	"""Nothing listening: None."""
	probe = socket.create_server(("127.0.0.1", 0))
	port = probe.getsockname()[1]
	probe.close()
	assert read_banner("127.0.0.1", port) is None


def test_a_finding_keeps_the_host_key_it_showed_without_storing_it():
	"""With host keys checked, the key a device showed rides on its finding
	(Add stores it) - the policy's own recording isn't called; the page's
	event shows its fingerprint, not the key."""
	stored = []
	key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGq0iqVb0x6jVg3x0eG7h1bQ2zJmPqvWQ7l5rWZ0aG5v"

	def detect(device):
		device.host_keys.seen(key)       # what _connect does after connecting
		return XE

	found = DiscoveryScan(parse_targets("10.0.0.1", 4096), 22, Login("u", "p", ""), skip=lambda t: None,
	                      knock=lambda a, p: BANNER, detect=detect, sleep=lambda s: None,
	                      host_keys=lambda target: HostKeyPolicy(learn=True, seen=stored.append))
	ended(found)
	(finding,) = found.findings()
	assert finding.host_key == key
	assert stored == []
	(event,) = [e for e in found.status()["events"] if e["kind"] == "finding"]
	assert event["host_key"] == fingerprint(key)


def test_a_changed_host_key_is_a_line_and_never_counts_toward_the_pause():
	"""A refused host key (nothing was sent): its own reason; even many of
	them don't pause the scan."""
	count = FAILURES_BEFORE_PAUSE + 3
	found, _ = make(f"10.0.0.1-{count}",
	                {f"10.0.0.{n}": HostKeyRefused(f"10.0.0.{n}:22", "ssh-rsa AAAA") for n in range(1, count + 1)})
	ended(found)
	phases = [e["phase"] for e in found.status()["events"] if e["kind"] == "phase"]
	assert phases == ["done"]
	assert found.counts()["reasons"]["host-key-changed"] == count
