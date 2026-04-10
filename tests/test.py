import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

# Add src to path so imports resolve without src. prefix (avoids dual module instances)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from validation import Validator
from logging_utils import RolloutLogger
from core import Device, RolloutOptions, RolloutEngine
from input_parser import InputParser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_device(**kwargs) -> Device:
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

class TestValidateIp(unittest.TestCase):

    def test_valid_ipv4(self):
        self.assertTrue(Validator.validate_ip("192.168.1.1"))

    def test_valid_ipv4_edge_zeros(self):
        self.assertTrue(Validator.validate_ip("0.0.0.0"))

    def test_valid_ipv4_broadcast(self):
        self.assertTrue(Validator.validate_ip("255.255.255.255"))

    def test_invalid_octet_out_of_range(self):
        self.assertFalse(Validator.validate_ip("999.1.1.1"))

    def test_invalid_missing_octet(self):
        self.assertFalse(Validator.validate_ip("192.168.1"))

    def test_invalid_empty_string(self):
        self.assertFalse(Validator.validate_ip(""))

    def test_invalid_hostname(self):
        self.assertFalse(Validator.validate_ip("router.local"))

    def test_invalid_with_port(self):
        self.assertFalse(Validator.validate_ip("192.168.1.1:22"))


class TestValidatePort(unittest.TestCase):

    def test_standard_ssh(self):
        self.assertTrue(Validator.validate_port("22"))

    def test_min_port(self):
        self.assertFalse(Validator.validate_port("0"))

    def test_max_port(self):
        self.assertTrue(Validator.validate_port("65535"))

    def test_above_max(self):
        self.assertFalse(Validator.validate_port("65536"))

    def test_negative(self):
        self.assertFalse(Validator.validate_port("-1"))

    def test_non_numeric(self):
        self.assertFalse(Validator.validate_port("ssh"))

    def test_float_string(self):
        self.assertFalse(Validator.validate_port("22.0"))

    def test_empty_string(self):
        self.assertFalse(Validator.validate_port(""))


class TestValidatePlatform(unittest.TestCase):

    def test_all_supported_platforms(self):
        for platform in Validator.SUPPORTED_PLATFORMS:
            with self.subTest(platform=platform):
                self.assertTrue(Validator.validate_platform(platform))

    def test_unsupported_platform(self):
        self.assertFalse(Validator.validate_platform("cisco_cat9k"))

    def test_empty_string(self):
        self.assertFalse(Validator.validate_platform(""))

    def test_case_sensitive(self):
        self.assertFalse(Validator.validate_platform("Cisco_IOS"))


class TestValidateDeviceData(unittest.TestCase):

    def setUp(self):
        self.validator = Validator(RolloutLogger(webapp=False, verbose=False))

    @staticmethod
    def _device(**overrides):
        base = {
            "ip": "10.0.0.1",
            "port": "22",
            "device_type": "cisco_ios",
            "username": "admin",
            "password": "pass",
            "secret": "s",
        }
        base.update(overrides)
        return base

    def test_valid_device(self):
        self.assertTrue(self.validator.validate_device_data(self._device()))

    def test_invalid_ip(self):
        self.assertFalse(self.validator.validate_device_data(self._device(ip="bad_ip")))

    def test_invalid_port(self):
        self.assertFalse(self.validator.validate_device_data(self._device(port="99999")))

    def test_invalid_platform(self):
        self.assertFalse(self.validator.validate_device_data(self._device(device_type="unknown")))

    def test_webapp_flag_does_not_affect_result(self):
        validator_web = Validator(RolloutLogger(webapp=True, verbose=False))
        self.assertTrue(validator_web.validate_device_data(self._device()))
        self.assertFalse(validator_web.validate_device_data(self._device(ip="x")))


class TestValidateFileExtension(unittest.TestCase):

    def setUp(self):
        self.validator = Validator(RolloutLogger(webapp=False, verbose=False))

    def test_valid_csv(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            self.assertTrue(self.validator.validate_file_extension(path, "csv"))
        finally:
            os.unlink(path)

    def test_valid_txt(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.assertTrue(self.validator.validate_file_extension(path, "txt"))
        finally:
            os.unlink(path)

    def test_wrong_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            self.assertFalse(self.validator.validate_file_extension(path, "txt"))
        finally:
            os.unlink(path)

    def test_file_not_found(self):
        self.assertFalse(self.validator.validate_file_extension("/nonexistent/path/file.csv", "csv"))

    def test_case_insensitive_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".CSV", delete=False) as f:
            path = f.name
        try:
            self.assertTrue(self.validator.validate_file_extension(path, "csv"))
        finally:
            os.unlink(path)


class TestTcpPort(unittest.TestCase):

    @patch("validation.socket.socket")
    def test_reachable_on_first_attempt(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.return_value = None
        self.assertTrue(Validator.test_tcp_port("10.0.0.1", 22))

    @patch("validation.socket.socket")
    def test_unreachable_after_all_retries(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("refused")
        with patch("validation.time.sleep"):
            self.assertFalse(Validator.test_tcp_port("10.0.0.1", 22))

    @patch("validation.socket.socket")
    def test_succeeds_on_second_attempt(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.side_effect = [OSError("refused"), None]
        with patch("validation.time.sleep"):
            self.assertTrue(Validator.test_tcp_port("10.0.0.1", 22))


# ---------------------------------------------------------------------------
# logging_utils.py
# ---------------------------------------------------------------------------

class TestMsg(unittest.TestCase):

    def test_no_color_terminal(self):
        logger = RolloutLogger(webapp=False, verbose=False)
        self.assertEqual(logger._msg("hello"), "hello")

    def test_red_terminal(self):
        logger = RolloutLogger(webapp=False, verbose=False)
        result = logger._msg("error", "red")
        self.assertIn("error", result)
        self.assertIn("\033[", result)

    def test_green_terminal(self):
        logger = RolloutLogger(webapp=False, verbose=False)
        result = logger._msg("ok", "green")
        self.assertIn("ok", result)
        self.assertIn("\033[", result)

    def test_webapp_red(self):
        logger = RolloutLogger(webapp=True, verbose=False)
        result = logger._msg("error", "red")
        self.assertIn("text-danger", result)
        self.assertIn("error", result)

    def test_webapp_green(self):
        logger = RolloutLogger(webapp=True, verbose=False)
        result = logger._msg("ok", "green")
        self.assertIn("text-success", result)

    def test_webapp_no_color(self):
        logger = RolloutLogger(webapp=True, verbose=False)
        self.assertEqual(logger._msg("plain"), "plain")

    def test_unknown_color_returns_plain(self):
        logger = RolloutLogger(webapp=False, verbose=False)
        self.assertEqual(logger._msg("hello", "purple"), "hello")


class TestLog(unittest.TestCase):

    def test_writes_message_to_file(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix="._log", delete=False) as f:
            path = f.name
        try:
            logger = RolloutLogger(webapp=False, verbose=False, logfile=path)
            logger._log("test message")
            with open(path) as f:
                content = f.read()
            self.assertIn("test message", content)
        finally:
            os.unlink(path)

    def test_includes_timestamp(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix="._log", delete=False) as f:
            path = f.name
        try:
            import re
            logger = RolloutLogger(webapp=False, verbose=False, logfile=path)
            logger._log("timestamped")
            with open(path) as f:
                content = f.read()
            self.assertRegex(content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        finally:
            os.unlink(path)

    def test_appends_multiple_entries(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix="._log", delete=False) as f:
            path = f.name
        try:
            logger = RolloutLogger(webapp=False, verbose=False, logfile=path)
            logger._log("first")
            logger._log("second")
            with open(path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
        finally:
            os.unlink(path)


class TestBaseNotify(unittest.TestCase):

    def setUp(self):
        f = tempfile.NamedTemporaryFile(mode="r", suffix="._log", delete=False)
        self.logfile = f.name
        f.close()

    def tearDown(self):
        os.unlink(self.logfile)

    def test_verbose_terminal_prints(self):
        logger = RolloutLogger(webapp=False, verbose=True, logfile=self.logfile)
        with patch("builtins.print") as mock_print:
            logger.notify("hello", "green")
            mock_print.assert_called_once()

    def test_non_verbose_terminal_does_not_print(self):
        logger = RolloutLogger(webapp=False, verbose=False, logfile=self.logfile)
        with patch("builtins.print") as mock_print:
            logger.notify("hello", "green")
            mock_print.assert_not_called()

    def test_verbose_webapp_enqueues(self):
        logger = RolloutLogger(webapp=True, verbose=True, logfile=self.logfile)
        logger.notify("hello", "green")
        self.assertFalse(logger._queue.empty())

    def test_non_verbose_webapp_does_not_enqueue(self):
        logger = RolloutLogger(webapp=True, verbose=False, logfile=self.logfile)
        logger.notify("hello", "green")
        self.assertTrue(logger._queue.empty())

    def test_always_logs_to_file(self):
        logger = RolloutLogger(webapp=False, verbose=False, logfile=self.logfile)
        logger.notify("logged")
        with open(self.logfile) as f:
            content = f.read()
        self.assertIn("logged", content)


# ---------------------------------------------------------------------------
# core.py — Device
# ---------------------------------------------------------------------------

class TestDeviceNetmikoConnector(unittest.TestCase):

    def test_returns_dict_with_all_fields(self):
        device = make_device()
        params = device.netmiko_connector()
        self.assertIsInstance(params, dict)
        for key in ("ip", "username", "password", "device_type", "port", "secret"):
            self.assertIn(key, params)

    def test_values_match_device_fields(self):
        device = make_device(ip="10.1.1.1", port=2222)
        params = device.netmiko_connector()
        self.assertEqual(params["ip"], "10.1.1.1")
        self.assertEqual(params["port"], 2222)


# class TestDeviceFetchConfig(unittest.TestCase):
#     TODO Step 2.6: rewrite when fetch_config receives injected RolloutLogger
#
#     def test_returns_config_string_on_success(self): ...
#     def test_returns_none_for_unsupported_platform(self): ...
#     def test_returns_none_on_connection_exception(self): ...

# (old body removed — Step 2.6 will rewrite)
class _TestDeviceFetchConfig_DISABLED(unittest.TestCase):

    def setUp(self):
        self.logger = RolloutLogger(webapp=False, verbose=False)

    def test_returns_config_string_on_success(self):
        device = make_device(device_type="cisco_ios")
        mock_driver = MagicMock()
        mock_node = MagicMock()
        mock_node.get_config.return_value = {"running": "interface GigabitEthernet0/0"}
        mock_driver.return_value = mock_node

        with patch("napalm.get_network_driver", return_value=mock_driver):
            result = device.fetch_config(self.logger)
        self.assertEqual(result, "interface GigabitEthernet0/0")

    def test_returns_none_for_unsupported_platform(self):
        device = make_device(device_type="checkpoint_gaia")
        result = device.fetch_config(self.logger)
        self.assertIsNone(result)

    def test_returns_none_on_connection_exception(self):
        device = make_device(device_type="cisco_ios")
        mock_driver = MagicMock()
        mock_node = MagicMock()
        mock_node.open.side_effect = Exception("timeout")
        mock_driver.return_value = mock_node

        with patch("napalm.get_network_driver", return_value=mock_driver):
            result = device.fetch_config(self.logger)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# core.py — prepare_devices
# TODO Phase 3: rewrite for InputParser.prepare_devices API
# ---------------------------------------------------------------------------

class TestPrepareDevices(unittest.TestCase):

    def setUp(self):
        logger = RolloutLogger(webapp=False, verbose=False)
        validator = Validator(logger)
        self.parser = InputParser(validator, logger)

    @staticmethod
    def _raw(**overrides):
        base = {
            "ip": "10.0.0.1",
            "username": "admin",
            "password": "pass",
            "device_type": "cisco_ios",
            "secret": "s",
            "port": "22",
        }
        base.update(overrides)
        return base

    @patch("validation.Validator.test_tcp_port", return_value=True)
    def test_valid_device_is_added(self, _):
        devices = self.parser.prepare_devices([self._raw()])
        self.assertEqual(len(devices), 1)
        self.assertIsInstance(devices[0], Device)

    @patch("validation.Validator.test_tcp_port", return_value=False)
    def test_unreachable_device_excluded(self, _):
        devices = self.parser.prepare_devices([self._raw()])
        self.assertEqual(len(devices), 0)

    @patch("validation.Validator.test_tcp_port", return_value=True)
    def test_invalid_ip_excluded(self, _):
        devices = self.parser.prepare_devices([self._raw(ip="bad")])
        self.assertEqual(len(devices), 0)

    @patch("validation.Validator.test_tcp_port", return_value=True)
    def test_device_type_lowercased(self, _):
        devices = self.parser.prepare_devices([self._raw(device_type="CISCO_IOS")])
        self.assertEqual(devices[0].device_type, "cisco_ios")

    @patch("validation.Validator.test_tcp_port", return_value=True)
    def test_multiple_devices(self, _):
        raw = [self._raw(ip=f"10.0.0.{i}") for i in range(1, 4)]
        devices = self.parser.prepare_devices(raw)
        self.assertEqual(len(devices), 3)


# ---------------------------------------------------------------------------
# core.py — parse_files
# TODO Phase 3: rewrite for InputParser.csv_to_inventory / parse_commands API
# ---------------------------------------------------------------------------

class TestParseFiles(unittest.TestCase):

    def setUp(self):
        self.logger = RolloutLogger(webapp=False, verbose=False)
        self.validator = Validator(self.logger)
        self.parser = InputParser(self.validator, self.logger)
        self.db_session = MagicMock()
        import uuid
        self.user_id = uuid.uuid4()

    @staticmethod
    def _write_csv(path, rows):
        with open(path, "w", encoding="utf-8") as f:
            f.write("ip,username,password,device_type,secret,port\n")
            for row in rows:
                f.write(",".join(str(row[k]) for k in
                                 ("ip", "username", "password",
                                  "device_type", "secret", "port")) + "\n")

    @staticmethod
    def _write_commands(path, commands):
        with open(path, "w") as f:
            f.write("\n".join(commands))

    @patch("validation.Validator.test_tcp_port", return_value=True)
    def test_csv_to_inventory_returns_devices(self, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            self._write_csv(csv_path, [
                {"ip": "10.0.0.1", "username": "admin", "password": "pass",
                 "device_type": "cisco_ios", "secret": "s", "port": "22"}
            ])
            devices = self.parser.csv_to_inventory(csv_path, self.user_id, self.db_session)
        self.assertEqual(len(devices), 1)
        self.assertIsInstance(devices[0], Device)

    def test_csv_to_inventory_nonexistent_file_returns_empty(self):
        devices = self.parser.csv_to_inventory("/no/such/file.csv", self.user_id, self.db_session)
        self.assertEqual(devices, [])

    def test_csv_to_inventory_wrong_extension_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = os.path.join(tmpdir, "devices.txt")
            open(bad_path, "w").close()
            devices = self.parser.csv_to_inventory(bad_path, self.user_id, self.db_session)
        self.assertEqual(devices, [])

    def test_csv_to_inventory_missing_columns_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            with open(csv_path, "w") as f:
                f.write("ip,username\n10.0.0.1,admin\n")
            devices = self.parser.csv_to_inventory(csv_path, self.user_id, self.db_session)
        self.assertEqual(devices, [])

    def test_parse_commands_returns_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            txt_path = os.path.join(tmpdir, "_commands.txt")
            self._write_commands(txt_path, ["ip route 0.0.0.0 0.0.0.0 10.0.0.254"])
            commands = self.parser.parse_commands(txt_path)
        self.assertEqual(len(commands), 1)
        self.assertIn("ip route", commands[0])

    def test_parse_commands_wrong_extension_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = os.path.join(tmpdir, "_commands.csv")
            open(bad_path, "w").close()
            commands = self.parser.parse_commands(bad_path)
        self.assertEqual(commands, [])

    def test_parse_commands_nonexistent_file_returns_empty(self):
        commands = self.parser.parse_commands("/no/such/_commands.txt")
        self.assertEqual(commands, [])


# ---------------------------------------------------------------------------
# core.py — RolloutEngine
# TODO Step 2.5: rewrite when RolloutEngine receives injected RolloutLogger
# ---------------------------------------------------------------------------

# class TestRolloutEngineNotify(unittest.TestCase):
#     TODO Step 2.5: rewrite — notify() replaced by injected RolloutLogger
#
#     def setUp(self):
#         while not LOG_QUEUE.empty():
#             LOG_QUEUE.get_nowait()
#
#     def test_verbose_terminal_prints(self): ...
#     def test_non_verbose_terminal_does_not_print(self): ...
#     def test_verbose_webapp_enqueues(self): ...


class _TestRolloutEnginePushConfig_DISABLED(unittest.TestCase):

    @staticmethod
    def _make_engine(devices=None, commands=None, cancel=None, **opt_kwargs):
        return RolloutEngine(
            param=make_options(**opt_kwargs),
            devices=devices or [make_device()],
            commands=commands or ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )

    def setUp(self):
        self.logger = RolloutLogger(webapp=False, verbose=False)
        self.cancel = threading.Event()

    @patch("netmiko.ConnectHandler")
    def test_successful_push_returns_none(self, mock_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_ch.return_value = mock_conn

        engine = self._make_engine()
        result = engine._push_config(self.cancel, self.logger)
        self.assertIsNone(result)
        mock_conn.save_config.assert_called_once()
        mock_conn.disconnect.assert_called_once()

    @patch("netmiko.ConnectHandler")
    def test_command_error_in_output_continues(self, mock_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "Invalid command"
        mock_ch.return_value = mock_conn

        engine = self._make_engine(commands=["bad command", "good command"])
        result = engine._push_config(self.cancel, self.logger)
        self.assertIsNone(result)
        # Both _commands were attempted despite first error
        self.assertEqual(mock_conn.send_config_set.call_count, 2)

    @patch("netmiko.ConnectHandler")
    def test_auth_failure_skips_device(self, mock_ch):
        import netmiko as nm
        mock_ch.side_effect = nm.NetMikoAuthenticationException("auth failed")
        engine = self._make_engine()
        result = engine._push_config(self.cancel, self.logger)
        self.assertIsNone(result)

    @patch("netmiko.ConnectHandler")
    def test_cancel_event_stops_rollout(self, mock_ch):
        cancel = threading.Event()
        cancel.set()
        engine = self._make_engine()
        result = engine._push_config(cancel, self.logger)
        self.assertEqual(result, "cancel_sent")
        mock_ch.assert_not_called()

    @patch("netmiko.ConnectHandler")
    def test_multiple_devices_all_attempted(self, mock_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_ch.return_value = mock_conn

        devices = [make_device(ip=f"10.0.0.{i}") for i in range(1, 4)]
        engine = self._make_engine(devices=devices)
        engine._push_config(self.cancel, self.logger)
        self.assertEqual(mock_ch.call_count, 3)


class _TestRolloutEngineVerify_DISABLED(unittest.TestCase):

    @staticmethod
    def _make_engine(devices=None, commands=None):
        return RolloutEngine(
            param=make_options(verify=True),
            devices=devices or [make_device()],
            commands=commands or ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )

    def setUp(self):
        self.logger = RolloutLogger(webapp=False, verbose=False)
        self.cancel = threading.Event()

    def test_command_found_in_config(self):
        device = make_device()
        engine = self._make_engine(
            devices=[device],
            commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )
        with patch.object(device, "fetch_config",
                          return_value="ip route 0.0.0.0 0.0.0.0 1.1.1.1"):
            result = engine._verify(self.logger)
        self.assertEqual(result["192.168.1.1"], 1)

    def test_command_not_in_config(self):
        device = make_device()
        engine = self._make_engine(
            devices=[device],
            commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )
        with patch.object(device, "fetch_config", return_value="no relevant config"):
            result = engine._verify(self.logger)
        self.assertEqual(result["192.168.1.1"], 0)

    def test_fetch_config_returns_none_skips_device(self):
        device = make_device()
        engine = self._make_engine(devices=[device])
        with patch.object(device, "fetch_config", return_value=None):
            result = engine._verify(self.logger)
        self.assertEqual(result.get("192.168.1.1", 0), 0)

    def test_cancel_event_stops_verify(self):
        cancel = threading.Event()
        cancel.set()
        engine = self._make_engine()
        result = engine._verify(self.logger)
        self.assertEqual(result, "cancel_sent")

    def test_partial_commands_matched(self):
        device = make_device()
        commands = ["ip route 0.0.0.0 0.0.0.0 1.1.1.1", "hostname ROUTER"]
        config = "ip route 0.0.0.0 0.0.0.0 1.1.1.1\nno relevant line"
        engine = self._make_engine(devices=[device], commands=commands)
        with patch.object(device, "fetch_config", return_value=config):
            result = engine._verify(self.logger)
        self.assertEqual(result["192.168.1.1"], 1)


class _TestRolloutEngineRun_DISABLED(unittest.TestCase):

    def setUp(self):
        self.logger = RolloutLogger(webapp=False, verbose=False)
        self.cancel = threading.Event()

    def test_empty_devices_returns_1(self):
        engine = RolloutEngine(
            param=make_options(),
            devices=[],
            commands=["cmd"],
        )
        self.assertEqual(engine.run(self.cancel, self.logger), 1)

    def test_empty_commands_returns_1(self):
        engine = RolloutEngine(
            param=make_options(),
            devices=[make_device()],
            commands=[],
        )
        self.assertEqual(engine.run(self.cancel, self.logger), 1)

    @patch("netmiko.ConnectHandler")
    def test_successful_run_without_verify_returns_0(self, mock_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_ch.return_value = mock_conn

        engine = RolloutEngine(
            param=make_options(verify=False),
            devices=[make_device()],
            commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )
        self.assertEqual(engine.run(self.cancel, self.logger), 0)

    @patch("netmiko.ConnectHandler")
    def test_cancel_during_push_returns_1(self, mock_ch):
        cancel = threading.Event()

        def fake_connect():
            cancel.set()
            raise Exception("cancelled")

        mock_ch.side_effect = fake_connect
        engine = RolloutEngine(
            param=make_options(),
            devices=[make_device()],
            commands=["cmd"],
        )
        result = engine.run(cancel, self.logger)
        # Either 0 (push finished before cancel seen) or 1 (cancel caught)
        self.assertIn(result, [0, 1])


# ---------------------------------------------------------------------------
# Integration — full rollout + verification pipeline
# TODO Phase 3: rewrite for inventory-based pipeline
# ---------------------------------------------------------------------------

class TestFullRolloutAndVerifyPipeline(unittest.TestCase):
    """
    End-to-end test of the full pipeline:
      import_from_inventory -> RolloutEngine.run() with _verify=True
    All network I/O is mocked: Netmiko SSH, NAPALM config fetch.
    Device.from_inventory is mocked because it is currently stubbed (returns None).
    """

    COMMAND = "ip route 0.0.0.0 0.0.0.0 10.0.0.254"

    def _make_inventory_row(self):
        """Return a minimal mock Inventory row."""
        return MagicMock()

    def _make_device(self):
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
    @patch("napalm.get_network_driver")
    @patch("core.Device.from_inventory")
    def test_full_pipeline_all_commands_verified(self, mock_from_inv, mock_napalm_driver, mock_netmiko_ch):
        device = self._make_device()
        mock_from_inv.return_value = device

        # Mock netmiko push
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_netmiko_ch.return_value = mock_conn

        # Mock napalm verify — config contains the command
        mock_driver = MagicMock()
        mock_node = MagicMock()
        mock_node.get_config.return_value = {"running": self.COMMAND}
        mock_driver.return_value = mock_node
        mock_napalm_driver.return_value = mock_driver

        inventory_rows = [self._make_inventory_row()]
        devices = InputParser.import_from_inventory(inventory_rows)

        engine = RolloutEngine(
            param=make_options(verify=True),
            devices=devices,
            commands=[self.COMMAND],
        )
        cancel = threading.Event()
        logger = RolloutLogger(webapp=False, verbose=False)
        result = engine.run(cancel, logger)
        self.assertEqual(result, 0)

    @patch("netmiko.ConnectHandler")
    @patch("core.Device.from_inventory")
    def test_full_pipeline_push_only_no_verify(self, mock_from_inv, mock_netmiko_ch):
        device = self._make_device()
        mock_from_inv.return_value = device

        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_netmiko_ch.return_value = mock_conn

        inventory_rows = [self._make_inventory_row()]
        devices = InputParser.import_from_inventory(inventory_rows)

        engine = RolloutEngine(
            param=make_options(verify=False),
            devices=devices,
            commands=[self.COMMAND],
        )
        cancel = threading.Event()
        logger = RolloutLogger(webapp=False, verbose=False)
        result = engine.run(cancel, logger)
        self.assertEqual(result, 0)

    @patch("netmiko.ConnectHandler")
    @patch("napalm.get_network_driver")
    @patch("core.Device.from_inventory")
    def test_full_pipeline_verify_fails_command_not_in_config(self, mock_from_inv, mock_napalm_driver, mock_netmiko_ch):
        device = self._make_device()
        mock_from_inv.return_value = device

        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_netmiko_ch.return_value = mock_conn

        # NAPALM returns config WITHOUT the command
        mock_driver = MagicMock()
        mock_node = MagicMock()
        mock_node.get_config.return_value = {"running": "no relevant config"}
        mock_driver.return_value = mock_node
        mock_napalm_driver.return_value = mock_driver

        inventory_rows = [self._make_inventory_row()]
        devices = InputParser.import_from_inventory(inventory_rows)

        engine = RolloutEngine(
            param=make_options(verify=True),
            devices=devices,
            commands=[self.COMMAND],
        )
        cancel = threading.Event()
        logger = RolloutLogger(webapp=False, verbose=False)
        # Pipeline completes (returns 0) even when verify finds mismatches
        result = engine.run(cancel, logger)
        self.assertEqual(result, 0)

    @patch("netmiko.ConnectHandler")
    @patch("core.Device.from_inventory")
    def test_full_pipeline_cancel_mid_rollout(self, mock_from_inv, mock_netmiko_ch):
        device = self._make_device()
        mock_from_inv.return_value = device

        cancel = threading.Event()

        def fake_connect(**kwargs):
            cancel.set()
            raise Exception("cancelled mid rollout")

        mock_netmiko_ch.side_effect = fake_connect

        inventory_rows = [self._make_inventory_row()]
        devices = InputParser.import_from_inventory(inventory_rows)

        engine = RolloutEngine(
            param=make_options(verify=False),
            devices=devices,
            commands=[self.COMMAND],
        )
        logger = RolloutLogger(webapp=False, verbose=False)
        result = engine.run(cancel, logger)
        # Cancel may be caught during push (returns 1) or push finishes first (returns 0)
        self.assertIn(result, [0, 1])


# ---------------------------------------------------------------------------
# Integration: rate limiting (requires live webapp on localhost:8080)
# Run manually: python tests/test.py RateLimitIntegrationTest
# Skipped automatically if the server is not reachable.
# ---------------------------------------------------------------------------

import urllib.request
import urllib.error
import urllib.parse


def _server_reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except Exception:
        return True  # any response (including 4xx) means server is up


@unittest.skipUnless(_server_reachable("http://localhost:8080/"), "webapp not running")
class RateLimitIntegrationTest(unittest.TestCase):
    """Sends 15 POST requests to /login and expects a 429 after the 10th."""

    URL = "http://localhost:8080/login"

    def test_login_rate_limit_triggers(self):
        import requests
        hit_429 = False
        for i in range(1, 16):
            r = requests.post(self.URL, data={"username": "test", "password": "test"},
                              allow_redirects=False)
            if r.status_code == 429:
                hit_429 = True
                self.assertLessEqual(i, 11,
                    f"Expected 429 by request 11, got it at request {i}")
                break
        self.assertTrue(hit_429, "Rate limiter never triggered after 15 requests")


if __name__ == "__main__":
    unittest.main()
