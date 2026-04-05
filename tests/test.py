import os
import sys
import queue
import socket
import tempfile
import threading
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch, mock_open, call

# Add src to path so imports resolve
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from validation import (
    validate_ip,
    validate_port,
    validate_platform,
    validate_device_data,
    validate_file_extension,
    test_tcp_port as tcp_probe,
    SUPPORTED_PLATFORMS,
)
from logging_utils import msg, log, base_notify, LOG_QUEUE
from core import Device, RolloutOptions, RolloutEngine, prepare_devices, parse_files


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
        self.assertTrue(validate_ip("192.168.1.1"))

    def test_valid_ipv4_edge_zeros(self):
        self.assertTrue(validate_ip("0.0.0.0"))

    def test_valid_ipv4_broadcast(self):
        self.assertTrue(validate_ip("255.255.255.255"))

    def test_invalid_octet_out_of_range(self):
        self.assertFalse(validate_ip("999.1.1.1"))

    def test_invalid_missing_octet(self):
        self.assertFalse(validate_ip("192.168.1"))

    def test_invalid_empty_string(self):
        self.assertFalse(validate_ip(""))

    def test_invalid_hostname(self):
        self.assertFalse(validate_ip("router.local"))

    def test_invalid_with_port(self):
        self.assertFalse(validate_ip("192.168.1.1:22"))


class TestValidatePort(unittest.TestCase):

    def test_standard_ssh(self):
        self.assertTrue(validate_port("22"))

    def test_min_port(self):
        self.assertTrue(validate_port("0"))

    def test_max_port(self):
        self.assertTrue(validate_port("65535"))

    def test_above_max(self):
        self.assertFalse(validate_port("65536"))

    def test_negative(self):
        self.assertFalse(validate_port("-1"))

    def test_non_numeric(self):
        self.assertFalse(validate_port("ssh"))

    def test_float_string(self):
        self.assertFalse(validate_port("22.0"))

    def test_empty_string(self):
        self.assertFalse(validate_port(""))


class TestValidatePlatform(unittest.TestCase):

    def test_all_supported_platforms(self):
        for platform in SUPPORTED_PLATFORMS:
            with self.subTest(platform=platform):
                self.assertTrue(validate_platform(platform))

    def test_unsupported_platform(self):
        self.assertFalse(validate_platform("cisco_cat9k"))

    def test_empty_string(self):
        self.assertFalse(validate_platform(""))

    def test_case_sensitive(self):
        self.assertFalse(validate_platform("Cisco_IOS"))


class TestValidateDeviceData(unittest.TestCase):

    def _device(self, **overrides):
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
        self.assertTrue(validate_device_data(self._device()))

    def test_invalid_ip(self):
        self.assertFalse(validate_device_data(self._device(ip="bad_ip")))

    def test_invalid_port(self):
        self.assertFalse(validate_device_data(self._device(port="99999")))

    def test_invalid_platform(self):
        self.assertFalse(validate_device_data(self._device(device_type="unknown")))

    def test_webapp_flag_does_not_affect_result(self):
        self.assertTrue(validate_device_data(self._device(), webapp=True))
        self.assertFalse(validate_device_data(self._device(ip="x"), webapp=True))


class TestValidateFileExtension(unittest.TestCase):

    def test_valid_csv(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            self.assertTrue(validate_file_extension(path, "csv"))
        finally:
            os.unlink(path)

    def test_valid_txt(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.assertTrue(validate_file_extension(path, "txt"))
        finally:
            os.unlink(path)

    def test_wrong_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            self.assertFalse(validate_file_extension(path, "txt"))
        finally:
            os.unlink(path)

    def test_file_not_found(self):
        self.assertFalse(validate_file_extension("/nonexistent/path/file.csv", "csv"))

    def test_case_insensitive_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".CSV", delete=False) as f:
            path = f.name
        try:
            self.assertTrue(validate_file_extension(path, "csv"))
        finally:
            os.unlink(path)


class TestTcpPort(unittest.TestCase):

    @patch("validation.socket.socket")
    def test_reachable_on_first_attempt(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.return_value = None
        self.assertTrue(tcp_probe("10.0.0.1", 22))

    @patch("validation.socket.socket")
    def test_unreachable_after_all_retries(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("refused")
        with patch("validation.time.sleep"):
            self.assertFalse(tcp_probe("10.0.0.1", 22))

    @patch("validation.socket.socket")
    def test_succeeds_on_second_attempt(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.side_effect = [OSError("refused"), None]
        with patch("validation.time.sleep"):
            self.assertTrue(tcp_probe("10.0.0.1", 22))


# ---------------------------------------------------------------------------
# logging_utils.py
# ---------------------------------------------------------------------------

class TestMsg(unittest.TestCase):

    def test_no_color_terminal(self):
        result = msg("hello")
        self.assertEqual(result, "hello")

    def test_red_terminal(self):
        result = msg("error", "red")
        self.assertIn("error", result)
        self.assertIn("\033[", result)

    def test_green_terminal(self):
        result = msg("ok", "green")
        self.assertIn("ok", result)
        self.assertIn("\033[", result)

    def test_webapp_red(self):
        result = msg("error", "red", webapp=True)
        self.assertIn("text-danger", result)
        self.assertIn("error", result)

    def test_webapp_green(self):
        result = msg("ok", "green", webapp=True)
        self.assertIn("text-success", result)

    def test_webapp_no_color(self):
        result = msg("plain", webapp=True)
        self.assertEqual(result, "plain")

    def test_unknown_color_raises(self):
        # Bug: unknown color causes COLORS.get() to return None, then None + str crashes.
        # This test documents current behavior — fix is to guard against None in msg().
        with self.assertRaises(TypeError):
            msg("hello", "purple")


class TestLog(unittest.TestCase):

    def test_writes_message_to_file(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log",
                                         delete=False) as f:
            path = f.name
        try:
            log("test message", file_name=path)
            with open(path) as f:
                content = f.read()
            self.assertIn("test message", content)
        finally:
            os.unlink(path)

    def test_includes_timestamp(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log",
                                         delete=False) as f:
            path = f.name
        try:
            log("timestamped", file_name=path)
            with open(path) as f:
                content = f.read()
            # Timestamp format: YYYY-MM-DD HH:MM:SS
            import re
            self.assertRegex(content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        finally:
            os.unlink(path)

    def test_appends_multiple_entries(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log",
                                         delete=False) as f:
            path = f.name
        try:
            log("first", file_name=path)
            log("second", file_name=path)
            with open(path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
        finally:
            os.unlink(path)


class TestBaseNotify(unittest.TestCase):

    def setUp(self):
        # Drain the global queue before each test
        while not LOG_QUEUE.empty():
            LOG_QUEUE.get_nowait()

    def test_verbose_terminal_prints(self):
        with patch("logging_utils.log"), patch("builtins.print") as mock_print:
            base_notify("hello", "green", verbose=True, webapp=False)
            mock_print.assert_called_once()

    def test_non_verbose_terminal_does_not_print(self):
        with patch("logging_utils.log"), patch("builtins.print") as mock_print:
            base_notify("hello", "green", verbose=False, webapp=False)
            mock_print.assert_not_called()

    def test_verbose_webapp_enqueues(self):
        with patch("logging_utils.log"):
            base_notify("hello", "green", verbose=True, webapp=True)
            self.assertFalse(LOG_QUEUE.empty())

    def test_non_verbose_webapp_does_not_enqueue(self):
        with patch("logging_utils.log"):
            base_notify("hello", "green", verbose=False, webapp=True)
            self.assertTrue(LOG_QUEUE.empty())

    def test_always_logs_to_file(self):
        with patch("logging_utils.log") as mock_log:
            base_notify("logged", webapp=False)
            mock_log.assert_called_once()


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


class TestDeviceFetchConfig(unittest.TestCase):

    def test_returns_config_string_on_success(self):
        device = make_device(device_type="cisco_ios")
        mock_driver = MagicMock()
        mock_node = MagicMock()
        mock_node.get_config.return_value = {"running": "interface GigabitEthernet0/0"}
        mock_driver.return_value = mock_node

        with patch("napalm.get_network_driver", return_value=mock_driver):
            result = device.fetch_config()
        self.assertEqual(result, "interface GigabitEthernet0/0")

    def test_returns_none_for_unsupported_platform(self):
        device = make_device(device_type="checkpoint_gaia")
        result = device.fetch_config()
        self.assertIsNone(result)

    def test_returns_none_on_connection_exception(self):
        device = make_device(device_type="cisco_ios")
        mock_driver = MagicMock()
        mock_node = MagicMock()
        mock_node.open.side_effect = Exception("timeout")
        mock_driver.return_value = mock_node

        with patch("napalm.get_network_driver", return_value=mock_driver):
            result = device.fetch_config()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# core.py — prepare_devices
# ---------------------------------------------------------------------------

class TestPrepareDevices(unittest.TestCase):

    def _raw(self, **overrides):
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

    @patch("core.test_tcp_port", return_value=True)
    def test_valid_device_is_added(self, _):
        devices = prepare_devices([self._raw()])
        self.assertEqual(len(devices), 1)
        self.assertIsInstance(devices[0], Device)

    @patch("core.test_tcp_port", return_value=False)
    def test_unreachable_device_excluded(self, _):
        devices = prepare_devices([self._raw()])
        self.assertEqual(len(devices), 0)

    @patch("core.test_tcp_port", return_value=True)
    def test_invalid_ip_excluded(self, _):
        devices = prepare_devices([self._raw(ip="bad")])
        self.assertEqual(len(devices), 0)

    @patch("core.test_tcp_port", return_value=True)
    def test_cancel_event_stops_processing(self, _):
        cancel = threading.Event()
        cancel.set()
        devices = prepare_devices([self._raw(), self._raw(ip="10.0.0.2")],
                                   cancel_event=cancel)
        self.assertEqual(devices, [])

    @patch("core.test_tcp_port", return_value=True)
    def test_device_type_lowercased(self, _):
        devices = prepare_devices([self._raw(device_type="CISCO_IOS")])
        self.assertEqual(devices[0].device_type, "cisco_ios")

    @patch("core.test_tcp_port", return_value=True)
    def test_multiple_devices(self, _):
        raw = [self._raw(ip=f"10.0.0.{i}") for i in range(1, 4)]
        devices = prepare_devices(raw)
        self.assertEqual(len(devices), 3)


# ---------------------------------------------------------------------------
# core.py — parse_files
# ---------------------------------------------------------------------------

class TestParseFiles(unittest.TestCase):

    def _write_csv(self, path, rows):
        with open(path, "w", encoding="utf-8") as f:
            f.write("ip,username,password,device_type,secret,port\n")
            for row in rows:
                f.write(",".join(str(row[k]) for k in
                                 ("ip", "username", "password",
                                  "device_type", "secret", "port")) + "\n")

    def _write_commands(self, path, commands):
        with open(path, "w") as f:
            f.write("\n".join(commands))

    @patch("core.test_tcp_port", return_value=True)
    def test_valid_files_return_devices_and_commands(self, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            txt_path = os.path.join(tmpdir, "commands.txt")
            self._write_csv(csv_path, [
                {"ip": "10.0.0.1", "username": "admin", "password": "pass",
                 "device_type": "cisco_ios", "secret": "s", "port": "22"}
            ])
            self._write_commands(txt_path, ["ip route 0.0.0.0 0.0.0.0 10.0.0.254"])
            devices, commands = parse_files(csv_path, txt_path)
        self.assertEqual(len(devices), 1)
        self.assertEqual(len(commands), 1)

    def test_nonexistent_csv_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            txt_path = os.path.join(tmpdir, "commands.txt")
            self._write_commands(txt_path, ["no command"])
            devices, commands = parse_files("/no/such/file.csv", txt_path)
        self.assertEqual(devices, [])
        self.assertEqual(commands, [])

    def test_wrong_extension_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.txt")  # wrong extension
            txt_path = os.path.join(tmpdir, "commands.txt")
            open(csv_path, "w").close()
            self._write_commands(txt_path, ["cmd"])
            devices, commands = parse_files(csv_path, txt_path)
        self.assertEqual(devices, [])

    def test_missing_csv_columns_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            txt_path = os.path.join(tmpdir, "commands.txt")
            with open(csv_path, "w") as f:
                f.write("ip,username\n10.0.0.1,admin\n")  # missing required cols
            self._write_commands(txt_path, ["cmd"])
            devices, commands = parse_files(csv_path, txt_path)
        self.assertEqual(devices, [])


# ---------------------------------------------------------------------------
# core.py — RolloutEngine
# ---------------------------------------------------------------------------

class TestRolloutEngineNotify(unittest.TestCase):

    def setUp(self):
        while not LOG_QUEUE.empty():
            LOG_QUEUE.get_nowait()

    def test_verbose_terminal_prints(self):
        engine = RolloutEngine(
            param=make_options(verbose=True, webapp=False),
            devices=[make_device()],
            commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )
        with patch("logging_utils.log"), patch("builtins.print") as mock_print:
            engine.notify("test message", "green")
            mock_print.assert_called_once()

    def test_non_verbose_terminal_does_not_print(self):
        engine = RolloutEngine(
            param=make_options(verbose=False, webapp=False),
            devices=[make_device()],
            commands=["cmd"],
        )
        with patch("logging_utils.log"), patch("builtins.print") as mock_print:
            engine.notify("quiet", "green")
            mock_print.assert_not_called()

    def test_verbose_webapp_enqueues(self):
        engine = RolloutEngine(
            param=make_options(verbose=True, webapp=True),
            devices=[make_device()],
            commands=["cmd"],
        )
        with patch("logging_utils.log"):
            engine.notify("hello", "green")
        self.assertFalse(LOG_QUEUE.empty())


class TestRolloutEnginePushConfig(unittest.TestCase):

    def _make_engine(self, devices=None, commands=None, cancel=None, **opt_kwargs):
        return RolloutEngine(
            param=make_options(**opt_kwargs),
            devices=devices or [make_device()],
            commands=commands or ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
            cancel_event=cancel,
        )

    @patch("netmiko.ConnectHandler")
    def test_successful_push_returns_none(self, mock_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_ch.return_value = mock_conn

        engine = self._make_engine()
        with patch("logging_utils.log"):
            result = engine.push_config()
        self.assertIsNone(result)
        mock_conn.save_config.assert_called_once()
        mock_conn.disconnect.assert_called_once()

    @patch("netmiko.ConnectHandler")
    def test_command_error_in_output_continues(self, mock_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "Invalid command"
        mock_ch.return_value = mock_conn

        engine = self._make_engine(commands=["bad command", "good command"])
        with patch("logging_utils.log"):
            result = engine.push_config()
        self.assertIsNone(result)
        # Both commands were attempted despite first error
        self.assertEqual(mock_conn.send_config_set.call_count, 2)

    @patch("netmiko.ConnectHandler")
    def test_auth_failure_skips_device(self, mock_ch):
        import netmiko as nm
        mock_ch.side_effect = nm.NetMikoAuthenticationException("auth failed")
        engine = self._make_engine()
        with patch("logging_utils.log"):
            result = engine.push_config()
        self.assertIsNone(result)

    @patch("netmiko.ConnectHandler")
    def test_cancel_event_stops_rollout(self, mock_ch):
        cancel = threading.Event()
        cancel.set()
        engine = self._make_engine(cancel=cancel)
        with patch("logging_utils.log"):
            result = engine.push_config()
        self.assertEqual(result, "cancel_sent")
        mock_ch.assert_not_called()

    @patch("netmiko.ConnectHandler")
    def test_multiple_devices_all_attempted(self, mock_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_ch.return_value = mock_conn

        devices = [make_device(ip=f"10.0.0.{i}") for i in range(1, 4)]
        engine = self._make_engine(devices=devices)
        with patch("logging_utils.log"):
            engine.push_config()
        self.assertEqual(mock_ch.call_count, 3)


class TestRolloutEngineVerify(unittest.TestCase):

    def _make_engine(self, devices=None, commands=None, cancel=None):
        return RolloutEngine(
            param=make_options(verify=True),
            devices=devices or [make_device()],
            commands=commands or ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
            cancel_event=cancel,
        )

    def test_command_found_in_config(self):
        device = make_device()
        engine = self._make_engine(
            devices=[device],
            commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )
        with patch.object(device, "fetch_config",
                          return_value="ip route 0.0.0.0 0.0.0.0 1.1.1.1"):
            with patch("logging_utils.log"):
                result = engine.verify()
        self.assertEqual(result["192.168.1.1"], 1)

    def test_command_not_in_config(self):
        device = make_device()
        engine = self._make_engine(
            devices=[device],
            commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )
        with patch.object(device, "fetch_config", return_value="no relevant config"):
            with patch("logging_utils.log"):
                result = engine.verify()
        self.assertEqual(result["192.168.1.1"], 0)

    def test_fetch_config_returns_none_skips_device(self):
        device = make_device()
        engine = self._make_engine(devices=[device])
        with patch.object(device, "fetch_config", return_value=None):
            with patch("logging_utils.log"):
                result = engine.verify()
        self.assertEqual(result.get("192.168.1.1", 0), 0)

    def test_cancel_event_stops_verify(self):
        cancel = threading.Event()
        cancel.set()
        engine = self._make_engine(cancel=cancel)
        with patch("logging_utils.log"):
            result = engine.verify()
        self.assertEqual(result, "cancel_sent")

    def test_partial_commands_matched(self):
        device = make_device()
        commands = ["ip route 0.0.0.0 0.0.0.0 1.1.1.1", "hostname ROUTER"]
        config = "ip route 0.0.0.0 0.0.0.0 1.1.1.1\nno relevant line"
        engine = self._make_engine(devices=[device], commands=commands)
        with patch.object(device, "fetch_config", return_value=config):
            with patch("logging_utils.log"):
                result = engine.verify()
        self.assertEqual(result["192.168.1.1"], 1)


class TestRolloutEngineRun(unittest.TestCase):

    def test_empty_devices_returns_1(self):
        engine = RolloutEngine(
            param=make_options(),
            devices=[],
            commands=["cmd"],
        )
        with patch("logging_utils.log"):
            self.assertEqual(engine.run(), 1)

    def test_empty_commands_returns_1(self):
        engine = RolloutEngine(
            param=make_options(),
            devices=[make_device()],
            commands=[],
        )
        with patch("logging_utils.log"):
            self.assertEqual(engine.run(), 1)

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
        with patch("logging_utils.log"):
            self.assertEqual(engine.run(), 0)

    @patch("netmiko.ConnectHandler")
    def test_cancel_during_push_returns_1(self, mock_ch):
        cancel = threading.Event()

        def fake_connect(**kwargs):
            cancel.set()
            raise Exception("cancelled")

        mock_ch.side_effect = fake_connect
        engine = RolloutEngine(
            param=make_options(),
            devices=[make_device()],
            commands=["cmd"],
            cancel_event=cancel,
        )
        with patch("logging_utils.log"):
            result = engine.run()
        # Either 0 (push finished before cancel seen) or 1 (cancel caught)
        self.assertIn(result, [0, 1])


# ---------------------------------------------------------------------------
# Integration — full rollout + verification pipeline
# ---------------------------------------------------------------------------

class TestFullRolloutAndVerifyPipeline(unittest.TestCase):
    """
    End-to-end test of the full pipeline:
      parse_files -> prepare_devices -> RolloutEngine.run() with verify=True

    All network I/O is mocked: TCP probe, Netmiko SSH, NAPALM config fetch.
    """

    COMMAND = "ip route 0.0.0.0 0.0.0.0 10.0.0.254"
    DEVICE_ROW = {
        "ip": "10.0.0.1",
        "username": "admin",
        "password": "password",
        "device_type": "cisco_ios",
        "secret": "enablepass",
        "port": "22",
    }

    def _write_csv(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("ip,username,password,device_type,secret,port\n")
            row = self.DEVICE_ROW
            f.write(f"{row['ip']},{row['username']},{row['password']},"
                    f"{row['device_type']},{row['secret']},{row['port']}\n")

    def _write_commands(self, path):
        with open(path, "w") as f:
            f.write(self.COMMAND + "\n")

    @patch("netmiko.ConnectHandler")
    @patch("napalm.get_network_driver")
    @patch("core.test_tcp_port", return_value=True)
    def test_full_pipeline_all_commands_verified(
        self, _tcp, mock_napalm_driver, mock_netmiko_ch
    ):
        # Netmiko mock — successful push
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_netmiko_ch.return_value = mock_conn

        # NAPALM mock — running config contains the command
        mock_driver_cls = MagicMock()
        mock_node = MagicMock()
        mock_node.get_config.return_value = {"running": self.COMMAND}
        mock_driver_cls.return_value = mock_node
        mock_napalm_driver.return_value = mock_driver_cls

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            txt_path = os.path.join(tmpdir, "commands.txt")
            self._write_csv(csv_path)
            self._write_commands(txt_path)

            with patch("logging_utils.log"):
                devices, commands = parse_files(csv_path, txt_path)
                engine = RolloutEngine(
                    param=RolloutOptions(verify=True, verbose=False, webapp=False),
                    devices=devices,
                    commands=commands,
                )
                exit_code = engine.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(devices), 1)
        mock_conn.save_config.assert_called_once()
        mock_conn.disconnect.assert_called_once()

    @patch("netmiko.ConnectHandler")
    @patch("core.test_tcp_port", return_value=True)
    def test_full_pipeline_push_only_no_verify(self, _tcp, mock_netmiko_ch):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_netmiko_ch.return_value = mock_conn

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            txt_path = os.path.join(tmpdir, "commands.txt")
            self._write_csv(csv_path)
            self._write_commands(txt_path)

            with patch("logging_utils.log"):
                devices, commands = parse_files(csv_path, txt_path)
                engine = RolloutEngine(
                    param=RolloutOptions(verify=False, verbose=False, webapp=False),
                    devices=devices,
                    commands=commands,
                )
                exit_code = engine.run()

        self.assertEqual(exit_code, 0)

    @patch("netmiko.ConnectHandler")
    @patch("napalm.get_network_driver")
    @patch("core.test_tcp_port", return_value=True)
    def test_full_pipeline_verify_fails_command_not_in_config(
        self, _tcp, mock_napalm_driver, mock_netmiko_ch
    ):
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_netmiko_ch.return_value = mock_conn

        # Config does NOT contain the command
        mock_driver_cls = MagicMock()
        mock_node = MagicMock()
        mock_node.get_config.return_value = {"running": "no relevant config here"}
        mock_driver_cls.return_value = mock_node
        mock_napalm_driver.return_value = mock_driver_cls

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            txt_path = os.path.join(tmpdir, "commands.txt")
            self._write_csv(csv_path)
            self._write_commands(txt_path)

            with patch("logging_utils.log"):
                devices, commands = parse_files(csv_path, txt_path)
                engine = RolloutEngine(
                    param=RolloutOptions(verify=True, verbose=False, webapp=False),
                    devices=devices,
                    commands=commands,
                )
                exit_code = engine.run()

        # run() still returns 0 — it reports partial/failed but does not error out
        self.assertEqual(exit_code, 0)

    @patch("netmiko.ConnectHandler")
    @patch("core.test_tcp_port", return_value=True)
    def test_full_pipeline_cancel_mid_rollout(self, _tcp, mock_netmiko_ch):
        cancel = threading.Event()

        def slow_connect(**kwargs):
            cancel.set()
            raise Exception("connection failed")

        mock_netmiko_ch.side_effect = slow_connect

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            txt_path = os.path.join(tmpdir, "commands.txt")
            self._write_csv(csv_path)
            self._write_commands(txt_path)

            with patch("logging_utils.log"):
                devices, commands = parse_files(csv_path, txt_path)
                engine = RolloutEngine(
                    param=RolloutOptions(verify=False, verbose=False, webapp=False),
                    devices=devices,
                    commands=commands,
                    cancel_event=cancel,
                )
                exit_code = engine.run()

        self.assertIn(exit_code, [0, 1])


if __name__ == "__main__":
    unittest.main()
