"""The rollout engine and its inputs: the pure input checks (validation), the
rollout logger, Device, InputParser's device and command files, and
RolloutEngine's push, verify and run - with Netmiko mocked."""
import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import MagicMock, patch

import netmiko as nm

# Import through the src package only — the app itself imports src.*, and a
# bare `import core` would load a second copy of every module (patches and
# isinstance checks would then silently target the wrong one).
from src import validation
from src.core import PushResult, VerifyResult, Device, RolloutOptions, RolloutEngine, endpoint
from src.input_parser import InputParser
from src.logging_utils import RolloutLogger
from src.platforms import FETCH_TIMEOUT, PLATFORMS
from src.validation import Validator


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

	def test_tcp_reachable_reaches_an_ipv6_device(self):
		"""The TCP probe (the CLI's reachability check, Test connection)
		connects to a device listening on an IPv6 address."""
		with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as server:
			server.bind(("::1", 0))
			server.listen(1)
			self.assertTrue(validation.tcp_reachable("::1", server.getsockname()[1]))


class TestValidateIp(unittest.TestCase):

	def test_valid_ipv4(self):
		"""A plain IPv4 address passes validate_ip."""
		self.assertTrue(validation.validate_ip("192.168.1.1"))

	def test_valid_ipv4_edge_zeros(self):
		"""0.0.0.0 passes validate_ip."""
		self.assertTrue(validation.validate_ip("0.0.0.0"))

	def test_valid_ipv4_broadcast(self):
		"""255.255.255.255 passes validate_ip."""
		self.assertTrue(validation.validate_ip("255.255.255.255"))

	def test_invalid_octet_out_of_range(self):
		"""An octet above 255 fails validate_ip."""
		self.assertFalse(validation.validate_ip("999.1.1.1"))

	def test_invalid_missing_octet(self):
		"""An address with only three octets fails validate_ip."""
		self.assertFalse(validation.validate_ip("192.168.1"))

	def test_invalid_empty_string(self):
		"""An empty string fails validate_ip."""
		self.assertFalse(validation.validate_ip(""))

	def test_invalid_hostname(self):
		"""A hostname fails validate_ip: only addresses are accepted."""
		self.assertFalse(validation.validate_ip("router.local"))

	def test_invalid_with_port(self):
		"""An address with a :port suffix fails validate_ip."""
		self.assertFalse(validation.validate_ip("192.168.1.1:22"))


class TestValidatePort(unittest.TestCase):

	def test_standard_ssh(self):
		"""Port 22 passes validate_port."""
		self.assertTrue(validation.validate_port("22"))

	def test_min_port(self):
		"""Port 0 fails validate_port; 1, the lowest, passes."""
		self.assertFalse(validation.validate_port("0"))
		self.assertTrue(validation.validate_port("1"))

	def test_max_port(self):
		"""Port 65535, the highest, passes validate_port."""
		self.assertTrue(validation.validate_port("65535"))

	def test_above_max(self):
		"""Port 65536 fails validate_port."""
		self.assertFalse(validation.validate_port("65536"))

	def test_negative(self):
		"""A negative port fails validate_port."""
		self.assertFalse(validation.validate_port("-1"))

	def test_non_numeric(self):
		"""A non-numeric port ("ssh") fails validate_port."""
		self.assertFalse(validation.validate_port("ssh"))

	def test_float_string(self):
		"""A decimal port ("22.0") fails validate_port."""
		self.assertFalse(validation.validate_port("22.0"))

	def test_empty_string(self):
		"""An empty string fails validate_port."""
		self.assertFalse(validation.validate_port(""))


class TestValidatePlatform(unittest.TestCase):

	def test_all_supported_platforms(self):
		"""Every platform in SUPPORTED_PLATFORMS passes validate_platform."""
		for platform in validation.SUPPORTED_PLATFORMS:
			with self.subTest(platform=platform):
				self.assertTrue(validation.validate_platform(platform))

	def test_unsupported_platform(self):
		"""A device type not in the supported list fails validate_platform."""
		self.assertFalse(validation.validate_platform("cisco_cat9k"))

	def test_empty_string(self):
		"""An empty string fails validate_platform."""
		self.assertFalse(validation.validate_platform(""))

	def test_case_sensitive(self):
		"""validate_platform is case-sensitive: "Cisco_IOS" fails."""
		self.assertFalse(validation.validate_platform("Cisco_IOS"))


class TestValidateDeviceData(unittest.TestCase):

	def setUp(self):
		self.validator = Validator(RolloutLogger(webapp=False, verbose=False))

	@staticmethod
	def _device(**overrides):
		"""A valid device row (cisco_ios, 10.0.0.1:22, credentials); overrides win."""
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
		"""A complete, valid device row passes validate_device_data."""
		self.assertTrue(self.validator.validate_device_data(self._device()))

	def test_invalid_ip(self):
		"""A row with an invalid ip fails validate_device_data."""
		self.assertFalse(self.validator.validate_device_data(self._device(ip="bad_ip")))

	def test_invalid_ip_is_reported_as_not_an_ip_address(self):
		"""The report for a bad IP says "not a valid IP address" - not "IPv4":
		an IPv6 address is accepted."""
		told = []
		with patch.object(self.validator._logger, "notify",
		                  side_effect=lambda msg, *a, **k: told.append(msg)):
			self.validator.validate_device_data(self._device(ip="bad_ip"))
		self.assertEqual(told, ["bad_ip is not a valid IP address"])
		self.assertTrue(self.validator.validate_device_data(self._device(ip="2001:db8::1")))

	def test_invalid_port(self):
		"""A row with an out-of-range port fails validate_device_data."""
		self.assertFalse(self.validator.validate_device_data(self._device(port="99999")))

	def test_invalid_platform(self):
		"""A row with an unknown device_type fails validate_device_data."""
		self.assertFalse(self.validator.validate_device_data(self._device(device_type="unknown")))

	def test_webapp_flag_does_not_affect_result(self):
		"""A web app logger gives the same verdicts: valid passes, a bad ip fails."""
		validator_web = Validator(RolloutLogger(webapp=True, verbose=False))
		self.assertTrue(validator_web.validate_device_data(self._device()))
		self.assertFalse(validator_web.validate_device_data(self._device(ip="x")))


class TestValidateFileExtension(unittest.TestCase):

	def setUp(self):
		self.validator = Validator(RolloutLogger(webapp=False, verbose=False))

	def test_valid_csv(self):
		"""An existing .csv file passes validate_file_extension for "csv"."""
		with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
			path = f.name
		try:
			self.assertTrue(self.validator.validate_file_extension(path, "csv"))
		finally:
			os.unlink(path)

	def test_valid_txt(self):
		"""An existing .txt file passes validate_file_extension for "txt"."""
		with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
			path = f.name
		try:
			self.assertTrue(self.validator.validate_file_extension(path, "txt"))
		finally:
			os.unlink(path)

	def test_wrong_extension(self):
		"""An existing .csv file fails validate_file_extension for "txt"."""
		with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
			path = f.name
		try:
			self.assertFalse(self.validator.validate_file_extension(path, "txt"))
		finally:
			os.unlink(path)

	def test_file_not_found(self):
		"""A path that doesn't exist fails validate_file_extension."""
		self.assertFalse(self.validator.validate_file_extension("/nonexistent/path/file.csv", "csv"))

	def test_case_insensitive_extension(self):
		"""The extension check ignores case: a .CSV file passes for "csv"."""
		with tempfile.NamedTemporaryFile(suffix=".CSV", delete=False) as f:
			path = f.name
		try:
			self.assertTrue(self.validator.validate_file_extension(path, "csv"))
		finally:
			os.unlink(path)


class TestTcpPort(unittest.TestCase):

	@patch("src.validation.socket.create_connection")
	def test_reachable_on_first_attempt(self, mock_connect):
		"""tcp_reachable is True when the first connect succeeds."""
		mock_connect.return_value = MagicMock()
		self.assertTrue(validation.tcp_reachable("10.0.0.1", 22))
		self.assertEqual(mock_connect.call_count, 1)

	@patch("src.validation.socket.create_connection")
	def test_unreachable_after_all_retries(self, mock_connect):
		"""tcp_reachable is False when every connect attempt is refused."""
		mock_connect.side_effect = OSError("refused")
		with patch("src.validation.time.sleep"):
			self.assertFalse(validation.tcp_reachable("10.0.0.1", 22))
		self.assertEqual(mock_connect.call_count, validation.TCP_RETRIES)

	@patch("src.validation.socket.create_connection")
	def test_succeeds_on_second_attempt(self, mock_connect):
		"""tcp_reachable retries: refused once, then connected, is True."""
		mock_connect.side_effect = [OSError("refused"), MagicMock()]
		with patch("src.validation.time.sleep"):
			self.assertTrue(validation.tcp_reachable("10.0.0.1", 22))


# ---------------------------------------------------------------------------
# logging_utils.py
# ---------------------------------------------------------------------------

class TestMsg(unittest.TestCase):

	def test_no_color_terminal(self):
		"""On the console, a message without a colour is returned unchanged."""
		logger = RolloutLogger(webapp=False, verbose=False)
		self.assertEqual(logger._msg("hello"), "hello")

	def test_red_terminal(self):
		"""On the console, red wraps the message in ANSI escape codes."""
		logger = RolloutLogger(webapp=False, verbose=False)
		result = logger._msg("error", "red")
		self.assertIn("error", result)
		self.assertIn("\033[", result)

	def test_green_terminal(self):
		"""On the console, green wraps the message in ANSI escape codes."""
		logger = RolloutLogger(webapp=False, verbose=False)
		result = logger._msg("ok", "green")
		self.assertIn("ok", result)
		self.assertIn("\033[", result)

	def test_webapp_red(self):
		"""In the web app, red wraps the message in the text-danger class."""
		logger = RolloutLogger(webapp=True, verbose=False)
		result = logger._msg("error", "red")
		self.assertIn("text-danger", result)
		self.assertIn("error", result)

	def test_webapp_green(self):
		"""In the web app, green wraps the message in the text-success class."""
		logger = RolloutLogger(webapp=True, verbose=False)
		result = logger._msg("ok", "green")
		self.assertIn("text-success", result)
		self.assertIn("ok", result)

	def test_webapp_no_color(self):
		"""In the web app, a message without a colour is returned unchanged."""
		logger = RolloutLogger(webapp=True, verbose=False)
		self.assertEqual(logger._msg("plain"), "plain")

	def test_unknown_color_returns_plain(self):
		"""An unknown colour name ("purple") leaves the message plain."""
		logger = RolloutLogger(webapp=False, verbose=False)
		self.assertEqual(logger._msg("hello", "purple"), "hello")


class TestLog(unittest.TestCase):

	def test_writes_message_to_file(self):
		"""_log writes the message into the logger's log file."""
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
		"""Each _log line carries a YYYY-MM-DD HH:MM:SS timestamp."""
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
		"""Two _log calls append two lines to the file."""
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
		"""On the console in verbose mode, notify prints the message once."""
		logger = RolloutLogger(webapp=False, verbose=True)
		logger.logfile = self.logfile
		with patch("builtins.print") as mock_print:
			logger.notify("hello", "green")
			mock_print.assert_called_once()

	def test_non_verbose_terminal_does_not_print(self):
		"""On the console without verbose, a plain green message isn't printed."""
		logger = RolloutLogger(webapp=False, verbose=False)
		logger.logfile = self.logfile
		with patch("builtins.print") as mock_print:
			logger.notify("hello", "green")
			mock_print.assert_not_called()

	def _webapp_logger(self, verbose):
		"""A web app logger for job-1 that streams to a mock Redis (history list +
		pub/sub channel); returns (logger, redis client)."""
		redis_client = MagicMock()
		logger = RolloutLogger(webapp=True, verbose=verbose, job_id="job-1",
							   redis_client=redis_client)
		logger.logfile = self.logfile
		return logger, redis_client

	def test_verbose_webapp_publishes(self):
		"""A verbose web app logger pushes the message to the job's history and
		publishes it once on job:job-1:logs."""
		logger, redis_client = self._webapp_logger(verbose=True)
		logger.notify("hello", "green")
		redis_client.publish.assert_called_once()
		redis_client.rpush.assert_called_once()
		self.assertEqual(redis_client.publish.call_args[0][0], "job:job-1:logs")

	def test_non_verbose_webapp_does_not_publish(self):
		"""A non-verbose web app logger keeps a plain green message out of Redis."""
		logger, redis_client = self._webapp_logger(verbose=False)
		logger.notify("hello", "green")
		redis_client.publish.assert_not_called()
		redis_client.rpush.assert_not_called()

	def test_non_verbose_webapp_publishes_errors_and_important(self):
		"""Without verbose, a red message and an important one are still published."""
		logger, redis_client = self._webapp_logger(verbose=False)
		logger.notify("boom", "red")
		logger.notify("milestone", important=True)
		self.assertEqual(redis_client.publish.call_count, 2)

	def test_webapp_without_job_id_never_touches_redis(self):
		"""A web app logger without a job id publishes nothing, even when important."""
		redis_client = MagicMock()
		logger = RolloutLogger(webapp=True, verbose=True,
							   redis_client=redis_client)
		logger.logfile = self.logfile
		logger.notify("hello", important=True)
		redis_client.publish.assert_not_called()

	def test_always_logs_to_file(self):
		"""notify writes the message to the log file even when it isn't shown."""
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
# core.py — prepare_devices
# ---------------------------------------------------------------------------

class TestPrepareDevices(unittest.TestCase):

	def setUp(self):
		logger = RolloutLogger(webapp=False, verbose=False)
		validator = Validator(logger)
		self.parser = InputParser(validator, logger)

	@staticmethod
	def _raw(**overrides):
		"""A valid raw CSV row (cisco_ios, 10.0.0.1:22, credentials); overrides win."""
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

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_valid_device_is_added(self, _):
		"""A valid, reachable row becomes one Device with no errors."""
		devices, errors = self.parser.prepare_devices([self._raw()])
		self.assertEqual(len(devices), 1)
		self.assertEqual(errors, [])
		self.assertIsInstance(devices[0], Device)

	@patch("src.validation.tcp_reachable", return_value=False)
	def test_unreachable_device_excluded(self, _):
		"""An unreachable device is left out, with the error
		"10.0.0.1:22 is not reachable"."""
		devices, errors = self.parser.prepare_devices([self._raw()])
		self.assertEqual(len(devices), 0)
		self.assertEqual(errors, ["10.0.0.1:22 is not reachable"])

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_invalid_ip_excluded(self, _):
		"""A row with an invalid ip gives no device."""
		devices, _ = self.parser.prepare_devices([self._raw(ip="bad")])
		self.assertEqual(len(devices), 0)

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_device_type_lowercased(self, _):
		"""The device type is lowercased: CISCO_IOS becomes cisco_ios."""
		devices, _ = self.parser.prepare_devices([self._raw(device_type="CISCO_IOS")])
		self.assertEqual(devices[0].device_type, "cisco_ios")

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_blank_cells_are_empty_values_not_missing_columns(self, _):
		"""Blank secret and label cells are accepted: the secret stays "" and the
		label falls back to the ip, with no errors."""
		devices, errors = self.parser.prepare_devices(
			[self._raw(secret="", label="")])
		self.assertEqual(errors, [])
		self.assertEqual((devices[0].secret, devices[0].label), ("", "10.0.0.1"))

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_short_rows_from_csv_reader_are_tolerated(self, _):
		"""A None cell (DictReader gives None for missing trailing cells) still
		gives one device and no errors."""
		row = self._raw()
		row["secret"] = None
		devices, errors = self.parser.prepare_devices([row])
		self.assertEqual((len(devices), errors), (1, []))

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_bad_row_is_reported_and_does_not_abort_the_rest(self, _):
		"""Of four rows, the bad-ip row 2 and the empty ip/port row 3 are reported
		by row number; rows 1 and 4 still become devices."""
		rows = [self._raw(ip="10.0.0.1"), self._raw(ip="bad"),
				self._raw(ip="", port=""), self._raw(ip="10.0.0.4")]
		devices, errors = self.parser.prepare_devices(rows)
		self.assertEqual([d.ip for d in devices], ["10.0.0.1", "10.0.0.4"])
		self.assertEqual(len(errors), 2)
		self.assertIn("Row 2", errors[0])
		self.assertIn("Row 3", errors[1])

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_credentials_required_only_when_asked(self, _):
		"""A row without credentials is refused ("username and password") by
		default, and accepted with require_credentials=False."""
		row = {"ip": "10.0.0.1", "device_type": "cisco_ios", "port": "22"}
		devices, errors = self.parser.prepare_devices([dict(row)])
		self.assertEqual(devices, [])
		self.assertIn("username and password", errors[0])
		devices, errors = self.parser.prepare_devices(
			[dict(row)], require_credentials=False)
		self.assertEqual((len(devices), errors), (1, []))

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_multiple_devices(self, _):
		"""Three valid rows give three devices."""
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

	@staticmethod
	def _write_commands(path, commands):
		with open(path, "w") as f:
			f.write("\n".join(commands))

	@patch("src.validation.tcp_reachable", return_value=True)
	def test_csv_to_inventory_returns_devices(self, _):
		"""csv_to_inventory turns a one-row devices CSV into one Device."""
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
		"""csv_to_inventory on a missing file gives no devices."""
		devices = self.parser.csv_to_inventory("/no/such/file.csv", self.user_id, self.db_session).devices
		self.assertEqual(devices, [])

	def test_csv_to_inventory_wrong_extension_returns_empty(self):
		"""csv_to_inventory on a .txt file gives no devices."""
		with tempfile.TemporaryDirectory() as tmpdir:
			bad_path = os.path.join(tmpdir, "devices.txt")
			open(bad_path, "w").close()
			devices = self.parser.csv_to_inventory(bad_path, self.user_id, self.db_session).devices
		self.assertEqual(devices, [])

	def test_csv_to_inventory_missing_columns_returns_empty(self):
		"""csv_to_inventory on a CSV missing required columns gives no devices."""
		with tempfile.TemporaryDirectory() as tmpdir:
			csv_path = os.path.join(tmpdir, "devices.csv")
			with open(csv_path, "w") as f:
				f.write("ip,username\n10.0.0.1,admin\n")
			devices = self.parser.csv_to_inventory(csv_path, self.user_id, self.db_session).devices
		self.assertEqual(devices, [])

	def test_parse_commands_returns_list(self):
		"""parse_commands reads a one-line .txt file into a one-command list."""
		with tempfile.TemporaryDirectory() as tmpdir:
			txt_path = os.path.join(tmpdir, "_commands.txt")
			self._write_commands(txt_path, ["ip route 0.0.0.0 0.0.0.0 10.0.0.254"])
			commands = self.parser.parse_commands(txt_path)
		self.assertEqual(len(commands), 1)
		self.assertIn("ip route", commands[0])

	def test_parse_commands_wrong_extension_returns_empty(self):
		"""parse_commands on a .csv file gives no commands."""
		with tempfile.TemporaryDirectory() as tmpdir:
			bad_path = os.path.join(tmpdir, "_commands.csv")
			open(bad_path, "w").close()
			commands = self.parser.parse_commands(bad_path)
		self.assertEqual(commands, [])

	def test_parse_commands_nonexistent_file_returns_empty(self):
		"""parse_commands on a missing file gives no commands."""
		commands = self.parser.parse_commands("/no/such/_commands.txt")
		self.assertEqual(commands, [])


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
	@patch("src.core.Device.from_inventory")
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
	@patch("src.core.Device.from_inventory")
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
	@patch("src.core.Device.from_inventory")
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
	@patch("src.core.Device.from_inventory")
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
	@patch("src.core.Device.from_inventory")
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


# The live-server rate-limit test that used to live here is replaced by a
# test-client version in tests/integration (it wrote failed logins into the
# live audit log and could lock localhost out of login for a minute).
