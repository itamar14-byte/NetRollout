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
# Import through the src package only — the app itself imports src.*, and a
# bare `import core` would load a second copy of every module (patches and
# isinstance checks would then silently target the wrong one).
from src.rollout import inputs
from src.rollout.engine import (PushResult, VerifyResult, Device, RolloutOptions, RolloutEngine,
                                endpoint, classify, mapping_resolvable)
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger
from src.rollout.platforms import FETCH_TIMEOUT, PLATFORMS


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
# core.py — Device
# ---------------------------------------------------------------------------

class TestDeviceNetmikoConnector(unittest.TestCase):

	def test_returns_dict_with_all_fields(self):
		"""netmiko_connector returns a dict with ip, username, password,
		device_type, port and secret."""
		device = make_device()
		params = device.netmiko_connector()
		self.assertIsInstance(params, dict)
		for key in ("ip", "username", "password", "device_type", "port", "secret"):
			self.assertIn(key, params)

	def test_values_match_device_fields(self):
		"""netmiko_connector carries the device's own ip and port."""
		device = make_device(ip="10.1.1.1", port=2222)
		params = device.netmiko_connector()
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
		result = make_device(device_type="cisco_ios").fetch_config(self.logger)
		self.assertEqual(result, "interface GigabitEthernet0/0")
		conn.send_command.assert_called_once_with("show running-config",
												  read_timeout=FETCH_TIMEOUT)

	@patch("netmiko.ConnectHandler")
	def test_uses_the_device_port(self, mock_ch):
		"""fetch_config connects on the device's own port (2201), not 22."""
		self._connection(mock_ch)
		make_device(port=2201).fetch_config(self.logger)   # port-forwarded
		self.assertEqual(mock_ch.call_args.kwargs["port"], 2201)

	@patch("netmiko.ConnectHandler")
	def test_every_platform_has_a_show_command(self, mock_ch):
		"""For every platform in PLATFORMS, fetch_config sends exactly that
		platform's show_config commands, in order."""
		for device_type, platform in PLATFORMS.items():
			conn = self._connection(mock_ch, output="set x")
			make_device(device_type=device_type).fetch_config(self.logger)
			sent = [c.args[0] for c in conn.send_command.call_args_list]
			self.assertEqual(sent, list(platform.show_config), device_type)

	@patch("netmiko.ConnectHandler")
	def test_returns_none_on_connection_exception(self, mock_ch):
		"""fetch_config returns None when the connection raises."""
		mock_ch.side_effect = Exception("timeout")
		self.assertIsNone(make_device().fetch_config(self.logger))


# ---------------------------------------------------------------------------
# core.py — RolloutEngine._push_config
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
		"""A clean push gives no cancel signal and PushResult(applied, 0 rejected);
		the config is saved and the connection closed once."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_ch.return_value = mock_conn

		engine = self._make_engine()
		cancel_signal, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIsNone(cancel_signal)
		self.assertEqual(push_results.get(0), PushResult(applied=True, rejected=0))
		mock_conn.save_config.assert_called_once()
		mock_conn.disconnect.assert_called_once()

	@patch("netmiko.ConnectHandler")
	def test_command_error_in_output_continues(self, mock_ch):
		"""A device answering "Invalid command" doesn't stop the push: both
		commands are sent and both counted as rejected (applied, 2 rejected)."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "Invalid command"
		mock_ch.return_value = mock_conn

		engine = self._make_engine(commands=["bad command", "good command"])
		cancel_signal, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIsNone(cancel_signal)
		# Both commands were attempted despite first error, both counted
		self.assertEqual(mock_conn.send_config_set.call_count, 2)
		self.assertEqual(push_results[0], PushResult(applied=True, rejected=2))

	@patch("netmiko.ConnectHandler")
	def test_auth_failure_marks_device_failed(self, mock_ch):
		"""An authentication failure marks the device not applied, without a
		cancel signal."""
		mock_ch.side_effect = nm.NetMikoAuthenticationException("auth failed")
		engine = self._make_engine()
		cancel_signal, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIsNone(cancel_signal)
		self.assertFalse(push_results[0].applied)

	@patch("netmiko.ConnectHandler")
	def test_cancel_event_stops_rollout(self, mock_ch):
		"""With the cancel event already set, the push returns "cancel_sent" and
		never connects to a device."""
		cancel = threading.Event()
		cancel.set()
		engine = self._make_engine()
		cancel_signal, push_results = engine._push_config(cancel, self.logger)
		self.assertEqual(cancel_signal, "cancel_sent")
		mock_ch.assert_not_called()

	def test_devices_finishing_after_cancel_are_still_recorded(self):
		"""A cancel stops devices that haven't connected yet; devices already
		mid-push finish (config applied) and must be recorded as pushed —
		not 'cancelled' — or rollback would skip them."""
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
			cancel_signal, push_results = engine._push_config(cancel,
															  self.logger)
		self.assertEqual(cancel_signal, "cancel_sent")
		# C (10.0.0.3) started after the cancel: never connected
		applied = PushResult(applied=True, rejected=0)
		self.assertEqual(push_results, {0: applied, 1: applied})  # by index

	@patch("netmiko.ConnectHandler")
	def test_multiple_devices_all_attempted(self, mock_ch):
		"""With three devices, each is connected to once, each has its result
		(applied), and no cancel is signalled."""
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_ch.return_value = mock_conn

		devices = [make_device(ip=f"10.0.0.{i}") for i in range(1, 4)]
		engine = self._make_engine(devices=devices)
		cancel_signal, push_results = engine._push_config(self.cancel, self.logger)
		self.assertIsNone(cancel_signal)
		self.assertEqual(mock_ch.call_count, 3)
		self.assertEqual(sorted(push_results), [0, 1, 2])
		self.assertTrue(all(r.applied for r in push_results.values()))


# ---------------------------------------------------------------------------
# core.py — RolloutEngine._verify
# ---------------------------------------------------------------------------

class TestRolloutEngineVerify(unittest.TestCase):

	@staticmethod
	def _make_engine(devices=None, commands=None):
		"""A verify-on engine for these devices and commands (one cisco_ios device
		and an ip route by default)."""
		return RolloutEngine(
			param=make_options(verify=True),
			devices=devices or [make_device()],
			commands=commands or ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
		)

	def setUp(self):
		self.logger = RolloutLogger(webapp=False, verbose=False)

	def test_command_found_in_config(self):
		"""A command present in the fetched config verifies (1 of 1), and no
		config snapshot is kept."""
		device = make_device()
		engine = self._make_engine(
			devices=[device],
			commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
		)
		with patch.object(device, "fetch_config",
						  return_value="ip route 0.0.0.0 0.0.0.0 1.1.1.1"):
			result = engine._verify([0], self.logger)
		# fully verified -> no config snapshot kept (nothing to diff)
		self.assertEqual(result[0], VerifyResult(verified=1, checkable=1,
												 config=None))

	def test_command_not_in_config(self):
		"""A command missing from the fetched config verifies 0 of 1."""
		device = make_device()
		engine = self._make_engine(
			devices=[device],
			commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
		)
		with patch.object(device, "fetch_config", return_value="no relevant config"):
			result = engine._verify([0], self.logger)
		self.assertEqual((result[0].verified, result[0].checkable), (0, 1))

	def test_config_not_fetched_is_not_a_failure(self):
		"""A config that can't be fetched gives no verify result (couldn't verify ≠
		not configured: the status then comes from the push), and the device is
		flagged "applied but NOT verified" for a person."""
		device = make_device()
		engine = self._make_engine(devices=[device])
		with patch.object(device, "fetch_config", return_value=None):
			result = engine._verify([0], self.logger)
		self.assertIsNone(result[0])
		# ...but nobody checked the result: a person must, and is told so
		(what,) = engine._needs_action[device.endpoint]
		self.assertIn("applied but NOT verified", what)

	def test_partial_commands_matched(self):
		"""One of two commands found verifies 1 of 2, and the fetched config is
		kept for Verify Diff."""
		device = make_device()
		commands = ["ip route 0.0.0.0 0.0.0.0 1.1.1.1", "hostname ROUTER"]
		config = "ip route 0.0.0.0 0.0.0.0 1.1.1.1\nno relevant line"
		engine = self._make_engine(devices=[device], commands=commands)
		with patch.object(device, "fetch_config", return_value=config):
			result = engine._verify([0], self.logger)
		# mismatch -> config snapshot kept for Verify Diff
		self.assertEqual(result[0], VerifyResult(verified=1, checkable=2,
												 config=config))


# ---------------------------------------------------------------------------
# core.py — RolloutEngine.run
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
		"""A clean push without verify gives one "success" result with
		commands_verified None."""
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
		self.assertEqual(result[0]["status"], "success")
		self.assertIsNone(result[0]["commands_verified"])

	def test_same_ip_different_ports_get_separate_results(self):
		"""Two devices on one IP with different ports (e.g. lab nodes
		port-forwarded behind one host IP) get their own results: 2001 success,
		2002 failed."""
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
		self.assertEqual(by_port, {2001: "success", 2002: "failed"})

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
		"1 success, 1 failed (of 2 devices)" (it used to say "2 devices
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
						 "Configuration rollout complete: 1 success, "
						 "1 failed (of 2 devices)")
		self.assertEqual(summary[0].args[1], "yellow")


# ---------------------------------------------------------------------------
# Integration — full rollout + verification pipeline
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
		devices = InputParser.import_from_inventory(inventory_rows, None)

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
	def test_full_pipeline_config_unreadable_is_action_needed(self, mock_from_inv, mock_netmiko_ch):
		"""The push works but the verify's config fetch can't log in: the result is
		"success" from the push, commands_verified None, action_needed "applied but
		NOT verified", and the log gives the reason and an ACTION NEEDED line."""
		device = self._make_device()
		mock_from_inv.return_value = device
		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_netmiko_ch.return_value = mock_conn
		mock_conn.__enter__.side_effect = Exception("Authentication failed")

		devices = InputParser.import_from_inventory([self._make_inventory_row()], None)
		engine = RolloutEngine(param=make_options(verify=True), devices=devices,
							   commands=[self.COMMAND])
		logger = RolloutLogger(webapp=False, verbose=False)
		(result,) = engine.run(threading.Event(), logger)
		# status from the push, but flagged for a person: the completion card,
		# Results and the Dashboard all read action_needed
		self.assertEqual(result["status"], "success")
		self.assertIsNone(result["commands_verified"])
		self.assertIn("applied but NOT verified", result["action_needed"])
		with open(logger.logfile, encoding="utf-8") as f:
			log = f.read()
		self.assertIn("could not fetch the config", log)       # the reason
		self.assertIn("ACTION NEEDED", log)

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_push_only_no_verify(self, mock_from_inv, mock_netmiko_ch):
		"""Push without verify: one "success" result with commands_verified None."""
		device = self._make_device()
		mock_from_inv.return_value = device

		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_netmiko_ch.return_value = mock_conn

		inventory_rows = [self._make_inventory_row()]
		devices = InputParser.import_from_inventory(inventory_rows, None)

		engine = RolloutEngine(
			param=make_options(verify=False),
			devices=devices,
			commands=[self.COMMAND],
		)
		cancel = threading.Event()
		logger = RolloutLogger(webapp=False, verbose=False)
		result = engine.run(cancel, logger)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["status"], "success")
		self.assertIsNone(result[0]["commands_verified"])

	@patch("netmiko.ConnectHandler")
	@patch("src.rollout.engine.Device.from_inventory")
	def test_full_pipeline_verify_fails_command_not_in_config(self, mock_from_inv, mock_netmiko_ch):
		"""Push, then a fetched config without the command: one "failed" result
		with 0 commands verified."""
		device = self._make_device()
		mock_from_inv.return_value = device

		mock_conn = MagicMock()
		mock_conn.send_config_set.return_value = "ok"
		mock_netmiko_ch.return_value = mock_conn

		mock_conn.__enter__.return_value = mock_conn      # the config fetch
		mock_conn.send_command.return_value = "no relevant config"

		inventory_rows = [self._make_inventory_row()]
		devices = InputParser.import_from_inventory(inventory_rows, None)

		engine = RolloutEngine(
			param=make_options(verify=True),
			devices=devices,
			commands=[self.COMMAND],
		)
		cancel = threading.Event()
		logger = RolloutLogger(webapp=False, verbose=False)
		result = engine.run(cancel, logger)
		self.assertEqual(len(result), 1)
		self.assertEqual(result[0]["status"], "failed")
		self.assertEqual(result[0]["commands_verified"], 0)

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
		devices = InputParser.import_from_inventory(inventory_rows, None)

		engine = RolloutEngine(
			param=make_options(verify=False),
			devices=devices,
			commands=[self.COMMAND],
		)
		logger = RolloutLogger(webapp=False, verbose=False)
		result = engine.run(cancel, logger)
		self.assertEqual([r["status"] for r in result], ["failed"])


PUSHED = PushResult(applied=True, rejected=0)


@pytest.mark.parametrize("push, verify, expected", [
	# never started / not applied: nothing counted
	(None, None, ("cancelled", 0, None)),
	(PushResult(applied=False, rejected=0), None, ("failed", 0, None)),
	(PushResult(applied=False, rejected=0), VerifyResult(5, 5, None),
	 ("failed", 0, None)),
	# verified: all confirmed, nothing refused
	(PUSHED, VerifyResult(5, 5, None), ("success", 5, 5)),
	# ... all confirmed but the device refused one → partial
	(PushResult(True, 1), VerifyResult(5, 5, None), ("partial", 5, 5)),
	# none confirmed → failed; some → partial
	(PUSHED, VerifyResult(0, 5, None), ("failed", 5, 0)),
	(PUSHED, VerifyResult(3, 5, None), ("partial", 5, 3)),
	# commands that can't be checked count as accounted for
	(PUSHED, VerifyResult(2, 3, None), ("partial", 5, 4)),
	(PUSHED, VerifyResult(3, 3, None), ("success", 5, 5)),
	# nothing checkable at all: success unless something was refused
	(PUSHED, VerifyResult(0, 0, None), ("success", 5, 5)),
	(PushResult(True, 1), VerifyResult(0, 0, None), ("partial", 5, 5)),
	# not verified: what the device said while the commands were sent
	(PUSHED, None, ("success", 5, None)),
	(PushResult(True, 2), None, ("partial", 5, None)),
	(PushResult(True, 4), None, ("failed", 5, None)),      # every configuring one
	(PushResult(True, 5), None, ("failed", 5, None)),
])
def test_status_rules(push, verify, expected):
	"""classify gives the expected (status, commands, verified) for each push and
	verify outcome, with 5 commands of which 4 configure something (one is
	navigation): not applied, verified, checkable or not, and push-only cases."""
	assert classify(push, verify, total=5, configuring=4) == expected


USER_A, USER_B, USER_C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture(autouse=True)
def cipher():
	"""A fresh encryption key for every test, so profiles can be encrypted."""
	os.environ[enc.ENV_VAR] = Fernet.generate_key().decode()
	enc.init_encryption(None)


def mapping(token, prop, user_id, index=None):
	return SimpleNamespace(token=token, property_name=prop, index=index,
	                       user_id=user_id)


def global_row(**overrides):
	"""A global inventory row (CORE-X) with an encrypted profile and the token
	$$HOST$$ bound by USER_A to hostname and by USER_B to vrfs[1]; overrides win."""
	row = SimpleNamespace(
		ip="10.50.0.1", label="CORE-X", device_type="cisco_ios", port=22,
		var_maps={"hostname": "core-x", "vrfs": ["red", "blue"]},
		security_profile=SimpleNamespace(
			username="netops", password_secret=enc.encrypt("pw"),
			enable_secret=enc.encrypt("en")),
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
	assert Device.from_inventory(row, USER_A).var_map_subs == \
	       {"$$HOST$$": ("hostname", None)}
	assert Device.from_inventory(row, USER_B).var_map_subs == \
	       {"$$HOST$$": ("vrfs", 1)}
	assert Device.from_inventory(row, USER_C).var_map_subs == {}


def test_substitution_uses_each_users_own_binding():
	"""The same command substitutes per user: "hostname core-x" for USER_A,
	"hostname blue" for USER_B, and the token untouched for USER_C."""
	row = global_row()
	engine = RolloutEngine(RolloutOptions(), [], ["hostname $$HOST$$"])
	assert engine._substitute_commands(Device.from_inventory(row, USER_A)) == \
	       ["hostname core-x"]
	assert engine._substitute_commands(Device.from_inventory(row, USER_B)) == \
	       ["hostname blue"]
	# a user with no binding pushes the token untouched
	assert engine._substitute_commands(Device.from_inventory(row, USER_C)) == \
	       ["hostname $$HOST$$"]


def test_credentials_are_decrypted_from_assigned_profile():
	"""The device's username, password and enable secret come decrypted from
	its security profile."""
	device = Device.from_inventory(global_row(), USER_C)
	assert (device.username, device.password, device.secret) == \
	       ("netops", "pw", "en")


def test_missing_enable_secret_becomes_empty_string():
	"""A profile without an enable secret gives the device secret ""."""
	row = global_row()
	row.security_profile.enable_secret = None
	assert Device.from_inventory(row, USER_A).secret == ""


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
	devices = [Device.from_inventory(r, USER_A) for r in (ok_row, broken_row)]
	engine = RolloutEngine(RolloutOptions(verify=True), devices,
	                       ["hostname $$HOST$$"])
	conn = MagicMock()
	conn.send_config_set.return_value = "ok"
	with patch("netmiko.ConnectHandler", return_value=conn) as connect, \
			patch.object(Device, "fetch_config", return_value="hostname core-x"):
		results = engine.run(threading.Event(), RolloutLogger(False, False))
	assert [c.kwargs["ip"] for c in connect.call_args_list] == ["10.0.0.1"]
	assert {r["device_ip"]: r["status"] for r in results} == \
	       {"10.0.0.1": "success", "10.0.0.2": "failed"}


def test_no_security_profile_raises():
	"""from_inventory on a row without a security profile raises ValueError
	("no security profiles")."""
	with pytest.raises(ValueError, match="no security profiles"):
		Device.from_inventory(global_row(security_profile=None), USER_A)
