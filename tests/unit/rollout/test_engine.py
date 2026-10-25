"""The rollout engine (src/rollout/engine.py): Device and its endpoint, Netmiko
connection details, config fetching, push / verify / run, the status rules
(classify) and per-device variable substitution."""
import os
import socket
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import netmiko as nm
import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
import src.rollout.engine as engine_module
# Import through the src package only - the app itself imports src.*, and a
# bare `import core` would load a second copy of every module (patches and
# isinstance checks would then silently target the wrong one).
from src.rollout import inputs
from src.rollout.engine import (SubstitutionError, Device, RolloutOptions, RolloutEngine,
                                endpoint, classify, mapping_resolvable, needs_decision)
from src.rollout.evidence import Judgment, Outcome, Verdict
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger
from src.rollout.platforms import FETCH_TIMEOUT, PLATFORMS
from src.rollout.session import NetmikoSession, PushResult, RunReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_device(**kwargs) -> Device:
	"""A cisco_ios Device on 192.168.1.1:22 with credentials; kwargs override."""
	defaults = dict(
		ip="192.168.1.1",
		username="admin",
		password="secret",
		device_type="cisco_ios",
		secret="enable_secret",
		port=22,
		label="test-device",
	)
	defaults.update(kwargs)
	return Device(**defaults)


def connect_params(device):
	"""The arguments the session hands Netmiko's ConnectHandler for this device."""
	with patch("netmiko.ConnectHandler") as connect:
		with NetmikoSession.connect(device):
			pass
	return connect.call_args.kwargs


def fetch_config(device, logger):
	"""The device's config, read by its session in a new SSH session (as after a
	push whose own session lost its prompt)."""
	return NetmikoSession(device, RunReport(logger))._fetch_new("in a test")


def make_options(**kwargs) -> RolloutOptions:
	defaults = dict(verify=False, verbose=False, webapp=False)
	defaults.update(kwargs)
	return RolloutOptions(**defaults)


# ---------------------------------------------------------------------------
# validation.py
# ---------------------------------------------------------------------------

class TestIPv6(unittest.TestCase):
	"""IPv6 devices: how an endpoint is written, and the TCP probe."""

	def test_an_endpoint_puts_an_ipv6_address_in_brackets(self):
		"""endpoint() writes ip:port for IPv4 and [ip]:port for IPv6 (as a URL
		does - the port can't be told from the address otherwise), and a
		Device's endpoint is the same."""
		self.assertEqual(endpoint("10.0.0.1", 22), "10.0.0.1:22")
		self.assertEqual(endpoint("2001:db8::1", 2222), "[2001:db8::1]:2222")
		self.assertEqual(make_device(ip="2001:db8::1").endpoint, "[2001:db8::1]:22")

	def test_a_target_is_the_box_a_hostname_resolved_to(self):
		"""Device.target: an IP is itself with its port (IPv6 in brackets); a
		hostname counts as the address its reachability check resolved it to,
		so a name and its IP are one target; a name the check didn't resolve
		is kept as connected to (the DNS suffix added); the port tells two
		boxes behind one address apart."""
		resolved = {"r1": "10.10.10.11"}
		self.assertEqual(make_device(ip="10.10.10.11").target(resolved), "10.10.10.11:22")
		self.assertEqual(make_device(ip="r1").target(resolved), "10.10.10.11:22")
		self.assertEqual(make_device(ip="r1", port=2222).target(resolved), "10.10.10.11:2222")
		self.assertEqual(make_device(ip="r2", dns_suffix="lab.local").target(resolved), "r2.lab.local:22")
		self.assertEqual(make_device(ip="r2").target(), "r2:22")
		self.assertEqual(make_device(ip="2001:db8::1").target(), "[2001:db8::1]:22")

	def test_an_address_is_kept_in_its_standard_form(self):
		"""normalize_ip writes an IPv6 address the one standard way (lower
		case, zeros compressed), so one device has one spelling; IPv4 is
		unchanged. The CSV rows (CLI and import) come out normalised too."""
		for typed in ("2001:DB8:0:0:0:0:0:1", "2001:0db8::0001", " 2001:db8::1 "):
			self.assertEqual(inputs.normalize_ip(typed), "2001:db8::1")
		self.assertEqual(inputs.normalize_ip("10.0.0.1"), "10.0.0.1")
		parser = InputParser(Validator(RolloutLogger(webapp=False, verbose=False)),
		                     RolloutLogger(webapp=False, verbose=False))
		(device,), errors = parser.prepare_devices(
			[{"ip": "2001:DB8:0::1", "port": "22", "device_type": "cisco_ios"}],
			require_credentials=False, check_reachable=False)
		self.assertEqual((device.ip, device.label, errors), ("2001:db8::1", "2001:db8::1", []))

	def test_tcp_reachable_reaches_an_ipv6_device(self):
		"""The TCP probe (the CLI's reachability check, Test connection)
		connects to a device listening on an IPv6 address."""
		with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as server:
			server.bind(("::1", 0))
			server.listen(1)
			self.assertTrue(inputs.tcp_reachable("::1", server.getsockname()[1]))


# ---------------------------------------------------------------------------
# core.py - Device
# ---------------------------------------------------------------------------

class TestDeviceNetmikoConnector(unittest.TestCase):

	def test_returns_dict_with_all_fields(self):
		"""The connect arguments are a dict with ip, username, password,
		device_type, port and secret."""
		device = make_device()
		params = connect_params(device)
		self.assertIsInstance(params, dict)
		for key in ("ip", "username", "password", "device_type", "port", "secret"):
			self.assertIn(key, params)

	def test_values_match_device_fields(self):
		"""The connect arguments carry the device's own ip and port."""
		device = make_device(ip="10.1.1.1", port=2222)
		params = connect_params(device)
		self.assertEqual(params["ip"], "10.1.1.1")
		self.assertEqual(params["port"], 2222)


class TestDeviceFetchConfig(unittest.TestCase):
	"""The config is fetched over Netmiko (the push's SSH): every platform,
    the device's own port, the platform's show command(s)."""

	def setUp(self):
		self.logger = RolloutLogger(webapp=False, verbose=False)

	@staticmethod
	def _connection(mock_ch, output="interface GigabitEthernet0/0"):
		"""A mock Netmiko connection at a CLI prompt whose show commands return
		output, installed as mock_ch's return value."""
		conn = MagicMock()
		conn.__enter__.return_value = conn
		conn.find_prompt.return_value = "dev>"      # a CLI prompt, not a shell
		conn.send_command.return_value = output
		mock_ch.return_value = conn
		return conn

	@patch("netmiko.ConnectHandler")
	def test_returns_config_string_on_success(self, mock_ch):
		"""fetch_config on cisco_ios sends "show running-config" once with
		FETCH_TIMEOUT and returns its output."""
		conn = self._connection(mock_ch)
		result = fetch_config(make_device(device_type="cisco_ios"), self.logger)
		self.assertEqual(result, "interface GigabitEthernet0/0")
		conn.send_command.assert_called_once_with("show running-config",
												  read_timeout=FETCH_TIMEOUT)

	@patch("netmiko.ConnectHandler")
	def test_uses_the_device_port(self, mock_ch):
		"""fetch_config connects on the device's own port (2201), not 22."""
		self._connection(mock_ch)
		fetch_config(make_device(port=2201), self.logger)   # port-forwarded
		self.assertEqual(mock_ch.call_args.kwargs["port"], 2201)

	@patch("netmiko.ConnectHandler")
	def test_every_platform_has_a_show_command(self, mock_ch):
		"""For every platform in PLATFORMS, fetch_config sends exactly that
		platform's show_config commands, in order - after its session
		preparation's own (IOS-XR: the time stamp off; PAN-OS: set format)."""
		prepared = {"cisco_xr": ["terminal exec prompt no-timestamp"],
		            "paloalto_panos": ["set cli config-output-format set"]}
		for device_type, platform in PLATFORMS.items():
			conn = self._connection(mock_ch, output="set x")
			fetch_config(make_device(device_type=device_type), self.logger)
			sent = [c.args[0] for c in conn.send_command.call_args_list]
			self.assertEqual(sent, prepared.get(device_type, []) + list(platform.show_config), device_type)

	@patch("netmiko.ConnectHandler")
	def test_returns_none_on_connection_exception(self, mock_ch):
		"""fetch_config returns None when the connection raises."""
		mock_ch.side_effect = Exception("timeout")
		self.assertIsNone(fetch_config(make_device(), self.logger))


# ---------------------------------------------------------------------------
# core.py - RolloutEngine._push_config
# ---------------------------------------------------------------------------

class TestRolloutEnginePushConfig(unittest.TestCase):

	@staticmethod
	def _make_engine(devices=None, commands=None, **opt_kwargs):
		"""An engine for these devices and commands (one cisco_ios device and an
		ip route by default); opt_kwargs go to the options."""
		return RolloutEngine(
			param=make_options(**opt_kwargs),
			devices=devices or [make_device()],
			commands=commands or ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
		)

	def setUp(self):
		self.logger = RolloutLogger(webapp=False, verbose=False)
		self.cancel = threading.Event()

	@patch("netmiko.ConnectHandler")
	def test_successful_push_no_cancel_signal(self, mock_ch):
		"""A clean push gives no cancel signal and an applied PushResult with
		the device's reply kept; the config is saved and the connection closed
		once."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_ch.return_value = mock_conn

		engine = self._make_engine()
		cancelled, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIs(cancelled, False)
		self.assertTrue(push_results[0].applied)
		self.assertEqual(push_results[0].replies, (("ok",),))
		mock_conn.save_config.assert_called_once()
		mock_conn.disconnect.assert_called_once()

	@patch("netmiko.ConnectHandler")
	def test_command_error_in_output_continues(self, mock_ch):
		"""A device answering "Invalid command" doesn't stop the push: both
		commands are sent and each answer is kept with its command (never
		judged: applied)."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "Invalid command"
		mock_ch.return_value = mock_conn

		engine = self._make_engine(commands=["bad command", "good command"])
		cancelled, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIs(cancelled, False)
		# Both commands were attempted despite first error, both answers kept
		self.assertEqual(mock_conn.send_config_set.call_count, 2)
		self.assertTrue(push_results[0].applied)
		self.assertEqual(push_results[0].replies, (("Invalid command",), ("Invalid command",)))

	@patch("netmiko.ConnectHandler")
	def test_auth_failure_marks_device_failed(self, mock_ch):
		"""An authentication failure marks the device not applied, without a
		cancel signal."""
		mock_ch.side_effect = nm.NetMikoAuthenticationException("auth failed")
		engine = self._make_engine()
		cancelled, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIs(cancelled, False)
		self.assertFalse(push_results[0].applied)

	@patch("netmiko.ConnectHandler")
	def test_cancel_event_stops_rollout(self, mock_ch):
		"""With the cancel event already set, the push returns cancelled and
		never connects to a device."""
		cancel = threading.Event()
		cancel.set()
		engine = self._make_engine()
		cancelled, push_results = engine._push_config(cancel, self.logger)
		self.assertIs(cancelled, True)
		mock_ch.assert_not_called()

	def test_devices_finishing_after_cancel_are_still_recorded(self):
		"""A cancel stops devices that haven't connected yet; devices already
		mid-push finish (config applied) and must be recorded as pushed -
		not 'cancelled' - or rollback would skip them."""
		cancel = threading.Event()
		b_connected = threading.Event()

		def connect(**params):
			conn = MagicMock()
			conn.send_config_set.return_value = "ok"
			if params["ip"] == "10.0.0.1":        # A: cancel arrives mid-push
				# only after B is past its cancel check (deterministic order)
				b_connected.wait(timeout=5)
				cancel.set()
				time.sleep(0.5)                   # finishes after C returns
			elif params["ip"] == "10.0.0.2":      # B: connected pre-cancel
				b_connected.set()
				time.sleep(0.1)                   # C starts once B frees a worker
			return conn

		devices = [make_device(ip=f"10.0.0.{i}") for i in (1, 2, 3)]
		engine = RolloutEngine(param=make_options(max_workers=2),
							   devices=devices, commands=["hostname x"])
		with patch("netmiko.ConnectHandler", side_effect=connect):
			cancelled, push_results = engine._push_config(cancel,
															  self.logger)
		self.assertIs(cancelled, True)
		# C (10.0.0.3) started after the cancel: never connected
		self.assertEqual({i: r.applied for i, r in push_results.items()},
		                 {0: True, 1: True})  # by index

	def test_ctrl_c_skips_the_devices_not_reached_yet(self):
		"""Ctrl+C in the CLI (KeyboardInterrupt in the waiting thread) cancels:
		the devices not started yet are skipped, those already being configured
		finish and are recorded, and the push returns cancelled - not every
		queued device pushed before the cancel (the thread pool's exit used to
		wait for all of them)."""
		pushed = []
		lock = threading.Lock()

		def push(device, cancel_event, report):
			if cancel_event.is_set():
				return None
			time.sleep(0.2)
			with lock:
				pushed.append(device.ip)
			return PushResult(applied=True)

		real = engine_module.as_completed
		pressed = threading.Event()        # one Ctrl+C, after the first result

		def interrupted(futures):
			for i, future in enumerate(real(futures)):
				if i == 1 and not pressed.is_set():
					pressed.set()
					raise KeyboardInterrupt
				yield future

		devices = [make_device(ip=f"10.0.1.{i}") for i in range(1, 31)]
		engine = RolloutEngine(param=make_options(max_workers=2), devices=devices,
		                       commands=["hostname x"])
		with patch.object(engine, "_push_device", side_effect=push), \
				patch.object(engine_module, "as_completed", interrupted):
			cancel = threading.Event()
			cancelled, push_results = engine._push_config(cancel, self.logger)
		self.assertIs(cancelled, True)
		self.assertTrue(cancel.is_set())
		self.assertLessEqual(len(pushed), 4)          # the first ones and those in flight
		self.assertEqual(len(push_results), len(pushed))   # each pushed device recorded

	@patch("netmiko.ConnectHandler")
	def test_multiple_devices_all_attempted(self, mock_ch):
		"""With three devices, each is connected to once, each has its result
		(applied), and no cancel is signalled."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_ch.return_value = mock_conn

		devices = [make_device(ip=f"10.0.0.{i}") for i in range(1, 4)]
		engine = self._make_engine(devices=devices)
		cancelled, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIs(cancelled, False)
		self.assertEqual(mock_ch.call_count, 3)
		self.assertEqual(sorted(push_results), [0, 1, 2])
		self.assertTrue(all(r.applied for r in push_results.values()))


# ---------------------------------------------------------------------------
# core.py - RolloutEngine.run
# ---------------------------------------------------------------------------

class TestRolloutEngineRun(unittest.TestCase):

	def setUp(self):
		self.logger = RolloutLogger(webapp=False, verbose=False)
		self.cancel = threading.Event()

	def test_empty_devices_returns_empty_list(self):
		"""run with no devices returns an empty result list."""
		engine = RolloutEngine(
			param=make_options(),
			devices=[],
			commands=["cmd"],
		)
		self.assertEqual(engine.run(self.cancel, self.logger), [])

	def test_empty_commands_returns_empty_list(self):
		"""run with no commands returns an empty result list."""
		engine = RolloutEngine(
			param=make_options(),
			devices=[make_device()],
			commands=[],
		)
		self.assertEqual(engine.run(self.cancel, self.logger), [])

	@patch("netmiko.ConnectHandler")
	def test_successful_run_without_verify(self, mock_ch):
		"""A clean push without verify gives one "not verified" result with
		commands_verified None (applied; nothing judged it)."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_ch.return_value = mock_conn

		engine = RolloutEngine(
			param=make_options(verify=False),
			devices=[make_device()],
			commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
		)
		result = engine.run(self.cancel, self.logger)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["status"], "not verified")
		self.assertIsNone(result[0]["commands_verified"])

	@patch("netmiko.ConnectHandler")
	def test_a_line_of_only_quotes_still_records_every_device(self, mock_ch):
		"""A command line of only double quotes ("" / " ") normalizes to
		nothing: the run still finishes and records a result for every device
		it configured, with and without verify (it used to raise IndexError
		after the push - no results recorded)."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_conn.send_command.return_value = "hostname x"     # the configuration
		mock_ch.return_value = mock_conn
		commands = ["hostname x", '""', '" "']
		for verify in (False, True):
			with self.subTest(verify=verify):
				devices = [make_device(ip="10.0.0.1"), make_device(ip="10.0.0.2")]
				engine = RolloutEngine(param=make_options(verify=verify),
				                       devices=devices, commands=commands)
				result = engine.run(self.cancel, self.logger)
				self.assertEqual(sorted(r["device_ip"] for r in result),
				                 ["10.0.0.1", "10.0.0.2"])
				self.assertTrue(all(r["commands_sent"] == 3 for r in result))

	def test_same_ip_different_ports_get_separate_results(self):
		"""Two devices on one IP with different ports (e.g. lab nodes
		port-forwarded behind one host IP) get their own results: 2001 applied
		(not verified: no verify), 2002 failed."""
		def connect(**params):
			if params["port"] == 2002:
				raise Exception("connection refused")
			conn = MagicMock()
			conn.send_config_set.return_value = "ok"
			return conn

		devices = [make_device(ip="10.9.9.9", port=2001, label="node-1"),
				   make_device(ip="10.9.9.9", port=2002, label="node-2")]
		engine = RolloutEngine(param=make_options(verify=False),
							   devices=devices, commands=["hostname x"])
		with patch("netmiko.ConnectHandler", side_effect=connect):
			result = engine.run(self.cancel, self.logger)
		by_port = {r["device_port"]: r["status"] for r in result}
		self.assertEqual(by_port, {2001: "not verified", 2002: "failed"})

	@patch("netmiko.ConnectHandler")
	def test_failed_push_marked_in_result(self, mock_ch):
		"""A device whose connection raises gets one "failed" result."""
		mock_ch.side_effect = Exception("connection refused")

		engine = RolloutEngine(
			param=make_options(verify=False),
			devices=[make_device()],
			commands=["cmd"],
		)
		result = engine.run(self.cancel, self.logger)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["status"], "failed")

	def test_summary_counts_real_outcomes(self):
		"""The summary line counts the real outcomes, once and in yellow:
		"1 failed, 1 not verified (of 2 devices)" (it used to say "2 devices
		configured" whatever happened)."""
		def connect(**params):
			if params["port"] == 2002:
				raise Exception("connection refused")
			conn = MagicMock()
			conn.send_config_set.return_value = "ok"
			return conn

		devices = [make_device(ip="10.9.9.9", port=2001),
				   make_device(ip="10.9.9.9", port=2002)]
		engine = RolloutEngine(param=make_options(verify=False),
							   devices=devices, commands=["hostname x"])
		with patch("netmiko.ConnectHandler", side_effect=connect), \
				patch.object(self.logger, "notify") as notify:
			engine.run(self.cancel, self.logger)
		summary = [c for c in notify.call_args_list
				   if "rollout complete" in c.args[0]]
		self.assertEqual(len(summary), 1)
		self.assertEqual(summary[0].args[0],
						 "Configuration rollout complete: 1 failed, "
						 "1 not verified (of 2 devices)")
		self.assertEqual(summary[0].args[1], "yellow")


# ---------------------------------------------------------------------------
# Integration - full rollout + verification pipeline
# ---------------------------------------------------------------------------

class TestFullRolloutAndVerifyPipeline(unittest.TestCase):
	"""
    End-to-end test of the full pipeline:
      import_from_inventory -> RolloutEngine.run() with verify=True
    All network I/O is mocked: Netmiko SSH for the push and the config fetch.
    Device.from_inventory is mocked because it requires a live DB redis_session.
    """

	COMMAND = "ip route 0.0.0.0 0.0.0.0 10.0.0.254"

	def _make_inventory_row(self):
		"""Return a minimal mock Inventory row."""
		return MagicMock()

	def _make_device(self):
		"""The cisco_ios device 10.0.0.1:22 (test-router) with credentials."""
		return make_device(
			ip="10.0.0.1",
			username="admin",
			password="password",
			device_type="cisco_ios",
			secret="enablepass",
			port=22,
			label="test-router",
		)

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_all_commands_verified(self, mock_from_inv, mock_netmiko_ch):
		"""Push, then a fetched config holding the command: one "success" result
		with 1 command verified."""
		device = self._make_device()
		mock_from_inv.return_value = device

		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_netmiko_ch.return_value = mock_conn

		mock_conn.__enter__.return_value = mock_conn      # the config fetch
		mock_conn.send_command.return_value = self.COMMAND

		inventory_rows = [self._make_inventory_row()]
		devices = InputParser.import_from_inventory(inventory_rows, None, {})

		engine = RolloutEngine(
			param=make_options(verify=True),
			devices=devices,
			commands=[self.COMMAND],
		)
		cancel = threading.Event()
		logger = RolloutLogger(webapp=False, verbose=False)
		result = engine.run(cancel, logger)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["status"], "success")
		self.assertEqual(result[0]["commands_verified"], 1)

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_config_unreadable_is_not_verified(self, mock_from_inv, mock_netmiko_ch):
		"""The push works but the configuration can't be read: the commands are
		still sent, the result is "not verified" (commands_verified None, no
		transcript), and the log gives the reason."""
		device = self._make_device()
		mock_from_inv.return_value = device
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_netmiko_ch.return_value = mock_conn
		mock_conn.send_command.side_effect = Exception("pattern not detected")

		devices = InputParser.import_from_inventory([self._make_inventory_row()], None, {})
		engine = RolloutEngine(param=make_options(verify=True), devices=devices,
							   commands=[self.COMMAND])
		logger = RolloutLogger(webapp=False, verbose=False)
		(result,) = engine.run(threading.Event(), logger)
		self.assertEqual(result["status"], "not verified")
		self.assertIsNone(result["commands_verified"])
		self.assertIsNone(result["transcript"])
		mock_conn.send_config_set.assert_called_once()           # still sent
		with open(logger.logfile, encoding="utf-8") as f:
			self.assertIn("couldn't read the configuration before the push", f.read())

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_push_only_no_verify(self, mock_from_inv, mock_netmiko_ch):
		"""Push without verify: one "not verified" result with commands_verified None."""
		device = self._make_device()
		mock_from_inv.return_value = device

		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_netmiko_ch.return_value = mock_conn

		inventory_rows = [self._make_inventory_row()]
		devices = InputParser.import_from_inventory(inventory_rows, None, {})

		engine = RolloutEngine(
			param=make_options(verify=False),
			devices=devices,
			commands=[self.COMMAND],
		)
		cancel = threading.Event()
		logger = RolloutLogger(webapp=False, verbose=False)
		result = engine.run(cancel, logger)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["status"], "not verified")
		self.assertIsNone(result[0]["commands_verified"])

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_command_not_in_config_is_review(self, mock_from_inv, mock_netmiko_ch):
		"""Push, then a configuration without the command (nothing changed), the
		device silent (only its echo): not proven either way - one "review"
		result with 0 commands proven."""
		device = self._make_device()
		mock_from_inv.return_value = device

		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = self.COMMAND
		mock_netmiko_ch.return_value = mock_conn

		mock_conn.__enter__.return_value = mock_conn      # the config fetch
		mock_conn.send_command.return_value = "no relevant config"

		inventory_rows = [self._make_inventory_row()]
		devices = InputParser.import_from_inventory(inventory_rows, None, {})

		engine = RolloutEngine(
			param=make_options(verify=True),
			devices=devices,
			commands=[self.COMMAND],
		)
		cancel = threading.Event()
		logger = RolloutLogger(webapp=False, verbose=False)
		result = engine.run(cancel, logger)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["status"], "review")
		self.assertEqual(result[0]["commands_verified"], 0)

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_unproven_command_the_device_replied_to_fails(self, mock_from_inv, mock_netmiko_ch):
		"""Push, nothing changed in the configuration, and the device answered the
		command: rejected - with no other command proven the device failed,
		0 commands proven, and its transcript says rejected."""
		device = self._make_device()
		mock_from_inv.return_value = device

		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = f"{self.COMMAND}\n% Invalid next hop"
		mock_netmiko_ch.return_value = mock_conn
		mock_conn.__enter__.return_value = mock_conn
		mock_conn.send_command.return_value = "no relevant config"

		devices = InputParser.import_from_inventory([self._make_inventory_row()], None, {})
		engine = RolloutEngine(param=make_options(verify=True), devices=devices, commands=[self.COMMAND])
		result = engine.run(threading.Event(), RolloutLogger(webapp=False, verbose=False))
		self.assertEqual(result[0]["status"], "failed")
		self.assertEqual(result[0]["commands_verified"], 0)
		self.assertEqual(result[0]["transcript"]["commands"][0]["verdict"], "rejected")

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_cancel_mid_rollout(self, mock_from_inv, mock_netmiko_ch):
		"""A cancel set while the device connects (which then fails) still records
		the device: one result, failed - it was already being connected to, so
		it isn't counted as cancelled."""
		device = self._make_device()
		mock_from_inv.return_value = device

		cancel = threading.Event()

		def fake_connect(**kwargs):
			cancel.set()
			raise Exception("cancelled mid rollout")

		mock_netmiko_ch.side_effect = fake_connect

		inventory_rows = [self._make_inventory_row()]
		devices = InputParser.import_from_inventory(inventory_rows, None, {})

		engine = RolloutEngine(
			param=make_options(verify=False),
			devices=devices,
			commands=[self.COMMAND],
		)
		logger = RolloutLogger(webapp=False, verbose=False)
		result = engine.run(cancel, logger)
		self.assertEqual([r["status"] for r in result], ["failed"])


def judged(*verdicts):
	""":returns: an outcome with these verdicts, one command each"""
	return Outcome(tuple(Judgment(f"c{i}", v) for i, v in enumerate(verdicts)), ())


REPLIES = ((), (), ())
T, P, R, N, V, X, E = (Verdict.TOOK_EFFECT, Verdict.IN_PLACE, Verdict.REMOVED,
                       Verdict.NAVIGATION, Verdict.REVIEW, Verdict.REJECTED, Verdict.EXEC)


@pytest.mark.parametrize("push, expected", [
	(None, ("cancelled", 0, None)),                                         # never started
	(PushResult(applied=False), ("failed", 0, None)),                        # not applied
	(PushResult(applied=True, sent=2, interrupted=True, replies=REPLIES[:2]),
	 ("partial", 2, None)),                                                  # stopped answering
	(PushResult(applied=True, replies=REPLIES), ("not verified", 3, None)),  # not judged
	(PushResult(applied=True, replies=REPLIES, outcome=judged(T, P, R)), ("success", 3, 3)),
	(PushResult(applied=True, replies=REPLIES, outcome=judged(T, N, P)), ("success", 3, 2)),
	(PushResult(applied=True, replies=REPLIES, outcome=judged(T, V, N)), ("review", 3, 1)),
	(PushResult(applied=True, replies=REPLIES, outcome=judged(V, V, V)), ("review", 3, 0)),
	(PushResult(applied=True, replies=REPLIES, outcome=judged(T, X, N)), ("partial", 3, 1)),
	(PushResult(applied=True, replies=REPLIES, outcome=judged(T, X, V)), ("partial", 3, 1)),
	(PushResult(applied=True, replies=REPLIES, outcome=judged(X, V, N)), ("failed", 3, 0)),
	(PushResult(applied=True, replies=REPLIES, outcome=judged(T, E, N)), ("success", 3, 1)),
])
def test_status_rules(push, expected):
	"""classify gives (status, commands sent, commands proven): cancelled,
	failed, partial, not verified - and, judged, success when every command is
	proven or navigation (or an exec command), review when any isn't; a rejected
	command makes it partial when another was proven, else failed."""
	assert classify(push) == expected


@pytest.mark.parametrize("transcript, expected", [
	(None, False),
	({"commands": [{"verdict": "took effect"}, {"verdict": "not a configuration command"}]}, False),
	({"commands": [{"verdict": "took effect"}, {"verdict": "review"}]}, True),
	({"commands": [{"verdict": "rejected"}]}, True),
])
def test_needs_decision(transcript, expected):
	"""A person decides a judged device with a command in review or rejected -
	never one without a transcript (interrupted, not judged)."""
	assert needs_decision(transcript) is expected


USER_A, USER_B, USER_C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture(autouse=True)
def cipher():
	"""A fresh encryption key for every test, so profiles can be encrypted."""
	os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
	enc.init_encryption(None)


def mapping(token, prop, user_id, index=None):
	return SimpleNamespace(token=token, property_name=prop, index=index,
	                       user_id=user_id)


def profile(enable_secret="en"):
	"""The profile (netops / pw, enable `enable_secret`) of the person rolling
	out, encrypted."""
	return SimpleNamespace(
		username="netops", password_secret=enc.encrypt("pw"),
		enable_secret=enc.encrypt(enable_secret) if enable_secret else None)


def global_row(**overrides):
	"""A global inventory row (CORE-X) with the token $$HOST$$ bound by USER_A
	to hostname and by USER_B to vrfs[1]; overrides win."""
	row = SimpleNamespace(
		ip="10.50.0.1", label="CORE-X", device_type="cisco_ios", port=22,
		var_maps={"hostname": "core-x", "vrfs": ["red", "blue"]},
		# same token bound by two users to different properties
		var_mappings=[mapping("$$HOST$$", "hostname", USER_A),
		              mapping("$$HOST$$", "vrfs", USER_B, index=1)])
	for k, v in overrides.items():
		setattr(row, k, v)
	return row


def test_only_rolling_out_users_mappings_are_applied():
	"""from_inventory keeps only the given user's mappings: USER_A gets hostname,
	USER_B vrfs[1], USER_C (no binding) none."""
	row = global_row()
	assert Device.from_inventory(row, USER_A, profile()).var_map_subs == \
	       {"$$HOST$$": ("hostname", None)}
	assert Device.from_inventory(row, USER_B, profile()).var_map_subs == \
	       {"$$HOST$$": ("vrfs", 1)}
	assert Device.from_inventory(row, USER_C, profile()).var_map_subs == {}


def test_substitution_uses_each_users_own_binding():
	"""The same command substitutes per user: "hostname core-x" for USER_A,
	"hostname blue" for USER_B; USER_C has no binding - refused, never the token
	pushed literally."""
	row = global_row()
	commands = ["hostname $$HOST$$"]
	assert Device.from_inventory(row, USER_A, profile()).commands_for(commands) == \
	       ["hostname core-x"]
	assert Device.from_inventory(row, USER_B, profile()).commands_for(commands) == \
	       ["hostname blue"]
	with pytest.raises(SubstitutionError, match=r"\$\$HOST\$\$: no mapping on this device"):
		Device.from_inventory(row, USER_C, profile()).commands_for(commands)


@pytest.mark.parametrize("commands, subs, extra, problems", [
	# a bound token whose value was cleared
	(["ip address $$LO$$ 255.255.255.255"], {"$$LO$$": ("loopback_ip", None)}, {},
	 ["$$LO$$: no value for 'loopback_ip'"]),
	# an index past the end of the list
	(["vrf $$VRF$$"], {"$$VRF$$": ("vrfs", 2)}, {"vrfs": ["red", "blue"]},
	 ["$$VRF$$: no value for 'vrfs[2]'"]),
	# a typo, and the wrong case (matching is exact)
	(["hostname $$HOSTNMAE$$", "snmp location $$site$$"], {"$$HOSTNAME$$": ("hostname", None)},
	 {"hostname": "r1"}, ["$$HOSTNMAE$$: no mapping on this device",
	                      "$$site$$: no mapping on this device"]),
	# a bound token the commands don't use isn't a problem (it failed the device)
	(["hostname r1"], {"$$LO$$": ("loopback_ip", None)}, {}, []),
	# a lone $$ isn't a token
	(["banner motd $$ hi $$"], {}, {}, []),
	(["hostname $$HOST$$"], {"$$HOST$$": ("hostname", None)}, {"hostname": "r1"}, []),
])
def test_unresolved_names_every_token_that_cant_be_filled(commands, subs, extra, problems):
	"""Device.unresolved lists, per device, each token in the commands with no value (or an
	index past the end) and each with no mapping on the device - in order, once each."""
	device = make_device(var_map_subs=subs, extra=extra)
	assert device.unresolved(commands) == problems


def test_a_device_with_a_leftover_token_is_skipped_at_push():
	"""At push time (a value gone while the job was queued, or the CLI) a device whose
	commands keep a token is skipped and failed without connecting - not sent
	"hostname $$HOSTNMAE$$"."""
	engine = RolloutEngine(RolloutOptions(), [make_device()], ["hostname $$HOSTNMAE$$"])
	logger = RolloutLogger(webapp=False, verbose=False)
	with patch("netmiko.ConnectHandler") as connect:
		_, results = engine._push_config(threading.Event(), logger)
	connect.assert_not_called()
	assert results[0] == PushResult(applied=False)


def test_credentials_are_decrypted_from_assigned_profile():
	"""The device's username, password and enable secret come decrypted from
	the security profile given - the person rolling out's own."""
	device = Device.from_inventory(global_row(), USER_C, profile())
	assert (device.username, device.password, device.secret) == \
	       ("netops", "pw", "en")


def test_missing_enable_secret_becomes_empty_string():
	"""A profile without an enable secret gives the device secret ""."""
	assert Device.from_inventory(global_row(), USER_A, profile(enable_secret=None)).secret == ""


@pytest.mark.parametrize("var_maps,prop,index,expected", [
	({"hostname": "r1"}, "hostname", None, True),
	({"hostname": ""}, "hostname", None, False),          # set but empty
	({}, "hostname", None, False),                        # missing
	(None, "hostname", None, False),
	({"vrfs": ["a", "b"]}, "vrfs", 1, True),
	({"vrfs": ["a", "b"]}, "vrfs", 2, False),             # index out of range
	({"vrfs": ["a"]}, "vrfs", -1, False),
	({"hostname": "r1"}, "hostname", 0, False),           # indexing a string
])
def test_mapping_resolvable(var_maps, prop, index, expected):
	"""mapping_resolvable is True only for a set, non-empty attribute (and, when
	indexed, a list long enough); empty, missing, None var_maps, out-of-range or
	negative indexes and indexing a string are False."""
	assert mapping_resolvable(var_maps, prop, index) is expected


def test_unresolvable_device_fails_alone_without_ssh(monkeypatch):
	"""A device whose mapping can't resolve fails alone: e.g. an admin removed an
	attribute from a global device after users bound mappings to it. It is never
	connected to and is "failed", while the other device is pushed, verified and
	"success"."""
	ok_row = global_row(ip="10.0.0.1", var_mappings=[
		mapping("$$HOST$$", "hostname", USER_A)])
	broken_row = global_row(ip="10.0.0.2", var_maps={}, var_mappings=[
		mapping("$$HOST$$", "hostname", USER_A)])
	devices = [Device.from_inventory(r, USER_A, profile()) for r in (ok_row, broken_row)]
	engine = RolloutEngine(RolloutOptions(verify=True), devices,
	                       ["hostname $$HOST$$"])
	conn = MagicMock()
	conn.send_config_set.return_value = "ok"
	reads = iter(["hostname old\n", "hostname core-x\n"])      # before, after
	conn.send_command.side_effect = lambda command, **_: (
		"hostname core-x\n" if command == "show startup-config" else next(reads))
	with patch("netmiko.ConnectHandler", return_value=conn) as connect:
		results = engine.run(threading.Event(), RolloutLogger(False, False))
	assert [c.kwargs["ip"] for c in connect.call_args_list] == ["10.0.0.1"]
	assert {r["device_ip"]: r["status"] for r in results} == \
	       {"10.0.0.1": "success", "10.0.0.2": "failed"}


def test_no_security_profile_raises():
	"""from_inventory without a security profile of the person rolling out
	raises ValueError naming the device."""
	with pytest.raises(ValueError, match="^you have no security profile on CORE-X$"):
		Device.from_inventory(global_row(), USER_A, None)


def test_an_action_needed_text_is_kept_redacted(tmp_path):
	"""RunReport.action_needed keeps the text for the database (Results, the
	summary) with the secret redacted, as the log line is."""
	logger = RolloutLogger(webapp=False, verbose=False)
	logger.logfile = str(tmp_path / "rollout.log")
	report = RunReport(logger)
	device = make_device(ip="10.0.0.1", device_type="fortinet")
	report.action_needed(device, "stopped answering after 'set psksecret ENC abc123=='")
	assert report.actions("10.0.0.1:22") == \
		"stopped answering after 'set psksecret ENC <redacted>'"
	assert "abc123" not in (tmp_path / "rollout.log").read_text(encoding="utf-8")


def summary_lines(statuses, verify=True, webapp=False):
	""":returns: the lines the engine's summary logs for devices with these statuses"""
	engine = RolloutEngine(RolloutOptions(verify=verify, webapp=webapp), [make_device()], ["x"])
	notifier = MagicMock(logfile="rollout.log")
	results = [{"device_ip": f"10.0.0.{i}", "device_port": 22, "status": s}
	           for i, s in enumerate(statuses, 1)]
	engine._log_summary(results, RunReport(notifier))
	return [c.args[0] for c in notifier.notify.call_args_list]


def test_the_summary_points_at_the_devices_to_review():
	"""Devices in review are named in one line saying what to check; the web
	app's says where to decide, the CLI's on the devices."""
	cli = summary_lines(["review", "success", "review"])
	assert ("REVIEW on 2 devices (10.0.0.1:22, 10.0.0.3:22) - NetRollout couldn't prove every "
	        "command there: check the lines marked review, then decide them on the devices") in cli
	web = summary_lines(["review"], webapp=True)
	assert any(line.endswith("decide them in Results → Verification") for line in web)
	assert not any(line.startswith("REVIEW") for line in summary_lines(["success"]))


def test_the_summary_says_why_devices_are_not_verified():
	"""Not verified, said with its reason: verification off (and how to turn it
	on), or the configuration that couldn't be read."""
	off = summary_lines(["not verified"], verify=False)
	assert any("verification was off" in line and "-vf" in line for line in off)
	on = summary_lines(["not verified"], verify=True)
	assert any("the configuration couldn't be read" in line for line in on)
	assert not any(line.startswith("Not verified") for line in summary_lines(["success"]))


def test_a_review_verdict_reaches_the_console_with_the_devices_reply():
	"""A command in review is logged as important (the CLI's console shows it
	without -v), with its evidence or note and the device's reply; a proven
	one is logged as usual."""
	notifier = MagicMock(logfile="x.log")
	outcome = Outcome((Judgment("ntp server 1.2.3.4", Verdict.REVIEW, (), "no change in its section"),
	                   Judgment("snmp-server location a", Verdict.TOOK_EFFECT,
	                            ("+ snmp-server location a",))), ())
	RolloutEngine._log_outcome(make_device(), outcome, (("% NTP failed",), ()), RunReport(notifier))
	(review, review_tone), review_kw = notifier.notify.call_args_list[0].args, notifier.notify.call_args_list[0].kwargs
	assert review == ("192.168.1.1:22: 'ntp server 1.2.3.4' review - no change in its section; "
	                  "the device replied: % NTP failed") and review_kw["important"] is True
	assert notifier.notify.call_args_list[1].kwargs["important"] is False
