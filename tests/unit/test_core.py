import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

# Import through the src package only — the app itself imports src.*, and a
# bare `import core` would load a second copy of every module (patches and
# isinstance checks would then silently target the wrong one).
from src.validation import Validator
from src.logging_utils import RolloutLogger
from src.core import PushResult, VerifyResult, Device, RolloutOptions, RolloutEngine
from src.input_parser import InputParser


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

    @patch("src.validation.socket.socket")
    def test_reachable_on_first_attempt(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.return_value = None
        self.assertTrue(Validator.test_tcp_port("10.0.0.1", 22))

    @patch("src.validation.socket.socket")
    def test_unreachable_after_all_retries(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("refused")
        with patch("src.validation.time.sleep"):
            self.assertFalse(Validator.test_tcp_port("10.0.0.1", 22))

    @patch("src.validation.socket.socket")
    def test_succeeds_on_second_attempt(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__.return_value = mock_sock
        mock_sock.connect.side_effect = [OSError("refused"), None]
        with patch("src.validation.time.sleep"):
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
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
            path = f.name
        try:
            logger = RolloutLogger(webapp=False, verbose=False)
            logger.logfile = path
            logger._log("test message")
            with open(path) as f:
                content = f.read()
            self.assertIn("test message", content)
        finally:
            os.unlink(path)

    def test_includes_timestamp(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
            path = f.name
        try:
            logger = RolloutLogger(webapp=False, verbose=False)
            logger.logfile = path
            logger._log("timestamped")
            with open(path) as f:
                content = f.read()
            self.assertRegex(content, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        finally:
            os.unlink(path)

    def test_appends_multiple_entries(self):
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as f:
            path = f.name
        try:
            logger = RolloutLogger(webapp=False, verbose=False)
            logger.logfile = path
            logger._log("first")
            logger._log("second")
            with open(path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
        finally:
            os.unlink(path)


class TestBaseNotify(unittest.TestCase):

    def setUp(self):
        f = tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False)
        self.logfile = f.name
        f.close()

    def tearDown(self):
        os.unlink(self.logfile)

    def test_verbose_terminal_prints(self):
        logger = RolloutLogger(webapp=False, verbose=True)
        logger.logfile = self.logfile
        with patch("builtins.print") as mock_print:
            logger.notify("hello", "green")
            mock_print.assert_called_once()

    def test_non_verbose_terminal_does_not_print(self):
        logger = RolloutLogger(webapp=False, verbose=False)
        logger.logfile = self.logfile
        with patch("builtins.print") as mock_print:
            logger.notify("hello", "green")
            mock_print.assert_not_called()

    def _webapp_logger(self, verbose):
        # Webapp mode streams to Redis (history list + pub/sub channel)
        redis_client = MagicMock()
        logger = RolloutLogger(webapp=True, verbose=verbose, job_id="job-1",
                               redis_client=redis_client)
        logger.logfile = self.logfile
        return logger, redis_client

    def test_verbose_webapp_publishes(self):
        logger, redis_client = self._webapp_logger(verbose=True)
        logger.notify("hello", "green")
        redis_client.publish.assert_called_once()
        redis_client.rpush.assert_called_once()
        self.assertEqual(redis_client.publish.call_args[0][0], "job:job-1:logs")

    def test_non_verbose_webapp_does_not_publish(self):
        logger, redis_client = self._webapp_logger(verbose=False)
        logger.notify("hello", "green")
        redis_client.publish.assert_not_called()
        redis_client.rpush.assert_not_called()

    def test_non_verbose_webapp_publishes_errors_and_important(self):
        logger, redis_client = self._webapp_logger(verbose=False)
        logger.notify("boom", "red")
        logger.notify("milestone", important=True)
        self.assertEqual(redis_client.publish.call_count, 2)

    def test_webapp_without_job_id_never_touches_redis(self):
        redis_client = MagicMock()
        logger = RolloutLogger(webapp=True, verbose=True,
                               redis_client=redis_client)
        logger.logfile = self.logfile
        logger.notify("hello", important=True)
        redis_client.publish.assert_not_called()

    def test_always_logs_to_file(self):
        logger = RolloutLogger(webapp=False, verbose=False)
        logger.logfile = self.logfile
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


class TestDeviceFetchConfig(unittest.TestCase):
    """The config is fetched over Netmiko (the push's SSH): every platform,
    the device's own port, the platform's show command(s)."""

    def setUp(self):
        self.logger = RolloutLogger(webapp=False, verbose=False)

    @staticmethod
    def _connection(mock_ch, output="interface GigabitEthernet0/0"):
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.send_command.return_value = output
        mock_ch.return_value = conn
        return conn

    @patch("netmiko.ConnectHandler")
    def test_returns_config_string_on_success(self, mock_ch):
        conn = self._connection(mock_ch)
        result = make_device(device_type="cisco_ios").fetch_config(self.logger)
        self.assertEqual(result, "interface GigabitEthernet0/0")
        conn.send_command.assert_called_once_with("show running-config",
                                                  read_timeout=60)

    @patch("netmiko.ConnectHandler")
    def test_uses_the_device_port(self, mock_ch):
        self._connection(mock_ch)
        make_device(port=2201).fetch_config(self.logger)   # port-forwarded
        self.assertEqual(mock_ch.call_args.kwargs["port"], 2201)

    @patch("netmiko.ConnectHandler")
    def test_every_platform_has_a_show_command(self, mock_ch):
        from src.core import PLATFORMS
        for device_type, platform in PLATFORMS.items():
            conn = self._connection(mock_ch, output="set x")
            make_device(device_type=device_type).fetch_config(self.logger)
            sent = [c.args[0] for c in conn.send_command.call_args_list]
            self.assertEqual(sent, list(platform.show_config), device_type)

    @patch("netmiko.ConnectHandler")
    def test_returns_none_on_connection_exception(self, mock_ch):
        mock_ch.side_effect = Exception("timeout")
        self.assertIsNone(make_device().fetch_config(self.logger))


# ---------------------------------------------------------------------------
# core.py — prepare_devices
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

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_valid_device_is_added(self, _):
        devices, errors = self.parser.prepare_devices([self._raw()])
        self.assertEqual(len(devices), 1)
        self.assertEqual(errors, [])
        self.assertIsInstance(devices[0], Device)

    @patch("src.validation.Validator.test_tcp_port", return_value=False)
    def test_unreachable_device_excluded(self, _):
        devices, errors = self.parser.prepare_devices([self._raw()])
        self.assertEqual(len(devices), 0)
        self.assertEqual(errors, ["10.0.0.1 is not reachable"])

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_invalid_ip_excluded(self, _):
        devices, _ = self.parser.prepare_devices([self._raw(ip="bad")])
        self.assertEqual(len(devices), 0)

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_device_type_lowercased(self, _):
        devices, _ = self.parser.prepare_devices([self._raw(device_type="CISCO_IOS")])
        self.assertEqual(devices[0].device_type, "cisco_ios")

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_blank_cells_are_empty_values_not_missing_columns(self, _):
        devices, errors = self.parser.prepare_devices(
            [self._raw(secret="", label="")])
        self.assertEqual(errors, [])
        self.assertEqual((devices[0].secret, devices[0].label), ("", "10.0.0.1"))

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_short_rows_from_csv_reader_are_tolerated(self, _):
        # DictReader gives None for missing trailing cells
        row = self._raw()
        row["secret"] = None
        devices, errors = self.parser.prepare_devices([row])
        self.assertEqual((len(devices), errors), (1, []))

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_bad_row_is_reported_and_does_not_abort_the_rest(self, _):
        rows = [self._raw(ip="10.0.0.1"), self._raw(ip="bad"),
                self._raw(ip="", port=""), self._raw(ip="10.0.0.4")]
        devices, errors = self.parser.prepare_devices(rows)
        self.assertEqual([d.ip for d in devices], ["10.0.0.1", "10.0.0.4"])
        self.assertEqual(len(errors), 2)
        self.assertIn("Row 2", errors[0])
        self.assertIn("Row 3", errors[1])

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_credentials_required_only_when_asked(self, _):
        row = {"ip": "10.0.0.1", "device_type": "cisco_ios", "port": "22"}
        devices, errors = self.parser.prepare_devices([dict(row)])
        self.assertEqual(devices, [])
        self.assertIn("username and password", errors[0])
        devices, errors = self.parser.prepare_devices(
            [dict(row)], require_credentials=False)
        self.assertEqual((len(devices), errors), (1, []))

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_multiple_devices(self, _):
        raw = [self._raw(ip=f"10.0.0.{i}") for i in range(1, 4)]
        devices, _ = self.parser.prepare_devices(raw)
        self.assertEqual(len(devices), 3)


# ---------------------------------------------------------------------------
# core.py — parse_files
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

    @patch("src.validation.Validator.test_tcp_port", return_value=True)
    def test_csv_to_inventory_returns_devices(self, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            self._write_csv(csv_path, [
                {"ip": "10.0.0.1", "username": "admin", "password": "pass",
                 "device_type": "cisco_ios", "secret": "s", "port": "22"}
            ])
            devices = self.parser.csv_to_inventory(csv_path, self.user_id, self.db_session).devices
        self.assertEqual(len(devices), 1)
        self.assertIsInstance(devices[0], Device)

    def test_csv_to_inventory_nonexistent_file_returns_empty(self):
        devices = self.parser.csv_to_inventory("/no/such/file.csv", self.user_id, self.db_session).devices
        self.assertEqual(devices, [])

    def test_csv_to_inventory_wrong_extension_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_path = os.path.join(tmpdir, "devices.txt")
            open(bad_path, "w").close()
            devices = self.parser.csv_to_inventory(bad_path, self.user_id, self.db_session).devices
        self.assertEqual(devices, [])

    def test_csv_to_inventory_missing_columns_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "devices.csv")
            with open(csv_path, "w") as f:
                f.write("ip,username\n10.0.0.1,admin\n")
            devices = self.parser.csv_to_inventory(csv_path, self.user_id, self.db_session).devices
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
# core.py — RolloutEngine._push_config
# ---------------------------------------------------------------------------

class TestRolloutEnginePushConfig(unittest.TestCase):

    @staticmethod
    def _make_engine(devices=None, commands=None, **opt_kwargs):
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
        import netmiko as nm
        mock_ch.side_effect = nm.NetMikoAuthenticationException("auth failed")
        engine = self._make_engine()
        cancel_signal, push_results = engine._push_config(self.cancel, self.logger)
        self.assertIsNone(cancel_signal)
        self.assertFalse(push_results[0].applied)

    @patch("netmiko.ConnectHandler")
    def test_cancel_event_stops_rollout(self, mock_ch):
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
        import time
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
        mock_conn = MagicMock()
        mock_conn.send_config_set.return_value = "ok"
        mock_ch.return_value = mock_conn

        devices = [make_device(ip=f"10.0.0.{i}") for i in range(1, 4)]
        engine = self._make_engine(devices=devices)
        cancel_signal, push_results = engine._push_config(self.cancel, self.logger)
        self.assertIsNone(cancel_signal)
        self.assertEqual(mock_ch.call_count, 3)


# ---------------------------------------------------------------------------
# core.py — RolloutEngine._verify
# ---------------------------------------------------------------------------

class TestRolloutEngineVerify(unittest.TestCase):

    @staticmethod
    def _make_engine(devices=None, commands=None):
        return RolloutEngine(
            param=make_options(verify=True),
            devices=devices or [make_device()],
            commands=commands or ["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )

    def setUp(self):
        self.logger = RolloutLogger(webapp=False, verbose=False)

    def test_command_found_in_config(self):
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
        device = make_device()
        engine = self._make_engine(
            devices=[device],
            commands=["ip route 0.0.0.0 0.0.0.0 1.1.1.1"],
        )
        with patch.object(device, "fetch_config", return_value="no relevant config"):
            result = engine._verify([0], self.logger)
        self.assertEqual((result[0].verified, result[0].checkable), (0, 1))

    def test_config_not_fetched_is_not_a_failure(self):
        # couldn't verify ≠ not configured: the status then comes from the push
        device = make_device()
        engine = self._make_engine(devices=[device])
        with patch.object(device, "fetch_config", return_value=None):
            result = engine._verify([0], self.logger)
        self.assertIsNone(result[0])

    def test_partial_commands_matched(self):
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
        engine = RolloutEngine(
            param=make_options(),
            devices=[],
            commands=["cmd"],
        )
        self.assertEqual(engine.run(self.cancel, self.logger), [])

    def test_empty_commands_returns_empty_list(self):
        engine = RolloutEngine(
            param=make_options(),
            devices=[make_device()],
            commands=[],
        )
        self.assertEqual(engine.run(self.cancel, self.logger), [])

    @patch("netmiko.ConnectHandler")
    def test_successful_run_without_verify(self, mock_ch):
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
        # e.g. lab nodes port-forwarded behind one host IP
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
        # it used to say "2 devices configured" whatever happened
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
    @patch("src.core.Device.from_inventory")
    def test_full_pipeline_all_commands_verified(self, mock_from_inv, mock_netmiko_ch):
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
    @patch("src.core.Device.from_inventory")
    def test_full_pipeline_push_only_no_verify(self, mock_from_inv, mock_netmiko_ch):
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
    @patch("src.core.Device.from_inventory")
    def test_full_pipeline_verify_fails_command_not_in_config(self, mock_from_inv, mock_netmiko_ch):
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
    @patch("src.core.Device.from_inventory")
    def test_full_pipeline_cancel_mid_rollout(self, mock_from_inv, mock_netmiko_ch):
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
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)


# The live-server rate-limit test that used to live here is replaced by a
# test-client version in tests/integration (it wrote failed logins into the
# live audit log and could lock localhost out of login for a minute).
