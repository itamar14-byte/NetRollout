"""A device's address (src/rollout/address.py and its users): what a hostname
is, the DNS suffix a bare name gets, bounded lookups, and where a name is
connected to - the session, the CLI's checks and the reachability probe."""
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.inventory import ReachabilityChecker, resolved_addresses
from src.rollout import address, inputs
from src.rollout.address import is_hostname, primary, qualify, resolve, resolve_many
from src.rollout.engine import Device
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger
from src.rollout.session import NetmikoSession, RunReport
from tests.unit.test_inventory import CountingProbe, FakeRedis


# ── what a hostname is ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
	"core1", "core1.corp.local", "CORE-1.Corp.Local", "core1.corp.local.",
	"a" * 63 + ".example", "sw-01.dc2", "r1.10net",
])
def test_a_valid_hostname(name):
	"""Labels of letters, digits and hyphens (1-63 each), a trailing dot
	allowed, a last label with a letter in it."""
	assert is_hostname(name) is True


@pytest.mark.parametrize("name", [
	"", ".", "-core1", "core1-", "core_1", "a..b", ".core1", "a" * 64,
	".".join(["a" * 63] * 4) + "x",     # 256 characters
	"10.0.0", "300.1.1.1", "1234",       # all digits at the end: a mistyped IP
	"core 1", "core1:22", "http://core1", "core1/24", "ŝwitch",
])
def test_not_a_hostname(name):
	"""Anything else is refused - an all-digits last label too (10.0.0 is
	a mistyped IP, not a name)."""
	assert is_hostname(name) is False


def test_validate_address_takes_an_ip_or_a_name():
	"""The input check: an IPv4, an IPv6 or a hostname passes; neither fails."""
	assert [inputs.validate_address(a) for a in
	        ("10.0.0.1", "2001:db8::1", "core1", "core1.corp.local", "core_1", "300.1.1.1")] \
		== [True, True, True, True, False, False]


def test_normalize_address_lowercases_a_name_and_compresses_an_ip():
	"""One spelling each: DNS ignores case; IPv6 compressed as before."""
	assert inputs.normalize_address(" Core1.CORP.local ") == "core1.corp.local"
	assert inputs.normalize_address("2001:DB8:0::1") == "2001:db8::1"
	with pytest.raises(ValueError):
		inputs.normalize_address("core_1")


# ── the DNS suffix ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("given, suffix, connected", [
	("core1", "corp.local", "core1.corp.local"),
	("core1", ".corp.local", "core1.corp.local"),     # a leading dot ignored
	("core1", "", "core1"),
	("core1.lab.local", "corp.local", "core1.lab.local"),
	("core1.", "corp.local", "core1."),              # absolute: as it is
	("10.0.0.1", "corp.local", "10.0.0.1"),
	("2001:db8::1", "corp.local", "2001:db8::1"),
])
def test_only_a_bare_name_gets_the_suffix(given, suffix, connected):
	"""qualify adds the suffix to a name without a domain; a full name or
	an IP is connected to as it is."""
	assert qualify(given, suffix) == connected


def test_a_device_connects_to_its_name_with_the_suffix():
	"""Device.host is what's connected to; its ip stays as saved (logs,
	results and labels show that)."""
	device = Device(ip="core1", label="core1", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22, dns_suffix="corp.local")
	assert (device.host, device.ip, device.endpoint) == ("core1.corp.local", "core1", "core1:22")


# ── lookups ──────────────────────────────────────────────────────────────────

def answers(*addresses):
	"""A getaddrinfo answering these addresses (each once per family/type, as
	a real one does)."""
	def getaddrinfo(host, port, *args, **kwargs):
		return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
		         (a, 0)) for a in addresses for _ in range(2)]
	return getaddrinfo


def test_an_ip_resolves_to_itself_without_a_lookup():
	"""No DNS for an IP: it's its own address (IPv6 in its one spelling)."""
	with patch.object(address.socket, "getaddrinfo", side_effect=AssertionError("looked up")):
		assert resolve("2001:DB8::1") == ["2001:db8::1"]


def test_a_name_resolves_to_its_addresses_in_order_each_once():
	"""The resolver's order kept, duplicates dropped, a zone dropped."""
	with patch.object(address.socket, "getaddrinfo", answers("10.0.0.7", "fe80::1%eth0", "10.0.0.7")):
		assert resolve("core1.corp.local") == ["10.0.0.7", "fe80::1"]


def test_a_name_that_doesnt_resolve_is_none():
	"""socket.gaierror (NXDOMAIN, no DNS) is None, not an exception."""
	with patch.object(address.socket, "getaddrinfo", side_effect=socket.gaierror(-2, "Name or service not known")):
		assert resolve("nope.corp.local") is None


def test_a_slow_lookup_is_not_waited_for():
	"""The system resolver can take many seconds to give up: after the
	timeout the name counts as not resolving, and the call returns."""
	release = threading.Event()

	def slow(*args, **kwargs):
		release.wait(5)
		return answers("10.0.0.7")(*args)
	try:
		with patch.object(address.socket, "getaddrinfo", side_effect=slow):
			started = time.monotonic()
			assert resolve("slow.corp.local", timeout=0.2) is None
			assert time.monotonic() - started < 1.5
	finally:
		release.set()


def test_the_address_kept_for_a_name_is_its_ipv4_one():
	"""A dual-stack name (localhost answers ::1 first) keeps its IPv4
	address, so the IP it's usually saved under matches; an IPv6-only name
	keeps its first; none: None."""
	assert primary(["::1", "127.0.0.1"]) == "127.0.0.1"
	assert primary(["2001:db8::2", "2001:db8::1"]) == "2001:db8::2"
	assert primary([]) is None


def test_resolve_many_looks_names_up_together():
	"""IPs answered at once, names each looked up once; a failure doesn't
	spoil the others."""
	asked = []

	def getaddrinfo(host, *args, **kwargs):
		asked.append(host)
		if host == "bad.corp.local":
			raise socket.gaierror(-2, "not known")
		return answers("10.1.1.1")(host, None)
	with patch.object(address.socket, "getaddrinfo", side_effect=getaddrinfo):
		found = resolve_many(["10.0.0.1", "a.corp.local", "bad.corp.local", "a.corp.local"])
	assert found == {"10.0.0.1": ["10.0.0.1"], "a.corp.local": ["10.1.1.1"], "bad.corp.local": None}
	assert sorted(asked) == ["a.corp.local", "bad.corp.local"]


# ── where a name is used ─────────────────────────────────────────────────────

def name_device(**kw):
	fields = dict(ip="core1", label="core1", username="u", password="p",
	              device_type="cisco_ios", secret="", port=22, dns_suffix="corp.local")
	fields.update(kw)
	return Device(**fields)


@patch("netmiko.ConnectHandler")
def test_the_session_connects_to_the_suffixed_name(connect):
	"""Netmiko is handed the name with the suffix."""
	with NetmikoSession.connect(name_device()):
		pass
	assert connect.call_args.kwargs["ip"] == "core1.corp.local"


@patch("netmiko.ConnectHandler")
def test_a_push_to_a_name_that_doesnt_resolve_says_so(connect):
	"""It fails before connecting, in words: "can't resolve" and the name
	looked up - not Netmiko's timeout or socket error."""
	told = []
	report = RunReport(MagicMock())
	with patch.object(report, "notify", side_effect=lambda text, *a, **k: told.append(text)), \
			patch("src.rollout.session.resolve", return_value=None) as looked_up:
		result = NetmikoSession(name_device(), report).push(["hostname x"])
	assert result.applied is False
	looked_up.assert_called_once_with("core1.corp.local")
	connect.assert_not_called()
	assert told[-1] == ("core1:22: can't resolve core1.corp.local - check the name and "
	                    "the DNS this server uses")


@patch("netmiko.ConnectHandler")
def test_a_push_to_an_ip_looks_nothing_up(connect):
	"""An IP is connected to directly - no lookup on the way."""
	connect.side_effect = Exception("stop here")
	with patch("src.rollout.session.resolve") as looked_up:
		NetmikoSession(name_device(ip="10.0.0.1"), RunReport(MagicMock())).push(["x"])
	looked_up.assert_not_called()
	assert connect.call_args.kwargs["ip"] == "10.0.0.1"


def test_the_cli_reports_a_name_that_doesnt_resolve_as_its_row():
	"""The CLI's checks before a push: a name that doesn't resolve is that
	row's error ("can't resolve"), the others go on."""
	logger = RolloutLogger(webapp=False, verbose=False)
	parser = InputParser(Validator(logger), logger)
	rows = [{"ip": ip, "device_type": "cisco_ios", "port": "22", "username": "u", "password": "p"}
	        for ip in ("10.0.0.1", "Ghost.corp.local")]
	with patch("src.rollout.inputs.resolve", side_effect=lambda h: None if "ghost" in h else [h]), \
			patch("src.rollout.inputs.tcp_reachable", return_value=True):
		devices, errors = parser.prepare_devices(rows)
	assert [d.ip for d in devices] == ["10.0.0.1"]
	assert errors == ["ghost.corp.local:22: can't resolve ghost.corp.local"]


def test_the_reachability_probe_resolves_with_the_suffix_and_caches_by_the_saved_name():
	"""A bare name is looked up with the suffix, then probed on its address;
	an IP is probed as it is. The cache key (and the answer) stays the
	address as saved, which is what the pages ask about; the name's answer
	carries the address it resolved to."""
	fake, probe, asked = FakeRedis(), CountingProbe(), []
	checker = ReachabilityChecker(lambda: fake, prober=probe, dns_suffix=lambda: "corp.local",
	                              resolver=lambda host: asked.append(host) or ["2001:db8::7", "10.0.0.7"])
	result = checker.check([("core1", 22), ("10.0.0.1", 22)])
	assert asked == ["core1.corp.local"]
	assert sorted(probe.calls) == [("10.0.0.1", 22), ("10.0.0.7", 22)]
	assert set(result) == {("core1", 22), ("10.0.0.1", 22)}
	assert result[("core1", 22)]["address"] == "10.0.0.7" and "address" not in result[("10.0.0.1", 22)]


def test_the_probe_goes_on_without_the_suffix_when_settings_cant_be_read():
	"""The setting unreadable (database down): no suffix, still a lookup and
	a probe."""
	fake, probe, asked = FakeRedis(), CountingProbe(), []

	def broken():
		raise RuntimeError("database down")
	ReachabilityChecker(lambda: fake, prober=probe, dns_suffix=broken,
	                    resolver=lambda host: asked.append(host) or ["10.0.0.9"]).check([("core1", 22)])
	assert (asked, probe.calls) == (["core1"], [("10.0.0.9", 22)])


def test_a_name_that_doesnt_resolve_is_unreachable_without_a_probe():
	"""No address, nothing to probe: not reachable, no address in the
	answer, and marked unresolved (the pages say "can't resolve"); an
	unreachable IP isn't marked."""
	fake, probe = FakeRedis(), CountingProbe(down={("10.0.0.9", 22)})
	result = ReachabilityChecker(lambda: fake, prober=probe, resolver=lambda host: None).check(
		[("ghost", 22), ("10.0.0.9", 22)])
	assert probe.calls == [("10.0.0.9", 22)]
	assert result[("ghost", 22)] == {"reachable": False, "checked_at": result[("ghost", 22)]["checked_at"],
	                                 "unresolved": True}
	assert "unresolved" not in result[("10.0.0.9", 22)] and result[("10.0.0.9", 22)]["reachable"] is False


def test_a_cached_answer_keeps_the_address_and_asks_no_dns():
	"""Within the cache's time a second check answers from Redis - the
	address included - without a lookup or a probe."""
	fake, probe, asked = FakeRedis(), CountingProbe(), []
	checker = ReachabilityChecker(lambda: fake, prober=probe,
	                              resolver=lambda host: asked.append(host) or ["10.0.0.7"])
	checker.check([("core1.corp.local", 22)])
	again = checker.check([("core1.corp.local", 22)])
	assert (len(asked), len(probe.calls)) == (1, 1)
	assert again[("core1.corp.local", 22)]["address"] == "10.0.0.7"


def test_resolved_addresses_maps_each_name_to_its_address():
	"""What a check resolved, by the name as saved - IPs and names without an
	address left out (what remember_addresses stores)."""
	results = {("core1", 22): {"reachable": True, "checked_at": "x", "address": "10.0.0.7"},
	           ("10.0.0.1", 22): {"reachable": True, "checked_at": "x"},
	           ("ghost", 22): {"reachable": False, "checked_at": "x"}}
	assert resolved_addresses(results) == {"core1": "10.0.0.7"}
