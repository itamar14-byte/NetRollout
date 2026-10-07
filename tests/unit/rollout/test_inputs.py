"""What a rollout is given (src/rollout/inputs.py): IP / port / platform /
file checks, the TCP probe, reading a devices CSV and a commands file."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import MagicMock, patch

# Import through the src package only — the app itself imports src.*, and a
# bare `import core` would load a second copy of every module (patches and
# isinstance checks would then silently target the wrong one).
from src.rollout import inputs as validation
from src.rollout.engine import Device
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger


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

	@patch("src.rollout.inputs.socket.create_connection")
	def test_reachable_on_first_attempt(self, mock_connect):
		"""tcp_reachable is True when the first connect succeeds."""
		mock_connect.return_value = MagicMock()
		self.assertTrue(validation.tcp_reachable("10.0.0.1", 22))
		self.assertEqual(mock_connect.call_count, 1)

	@patch("src.rollout.inputs.socket.create_connection")
	def test_unreachable_after_all_retries(self, mock_connect):
		"""tcp_reachable is False when every connect attempt is refused."""
		mock_connect.side_effect = OSError("refused")
		with patch("src.rollout.inputs.time.sleep"):
			self.assertFalse(validation.tcp_reachable("10.0.0.1", 22))
		self.assertEqual(mock_connect.call_count, validation.TCP_RETRIES)

	@patch("src.rollout.inputs.socket.create_connection")
	def test_succeeds_on_second_attempt(self, mock_connect):
		"""tcp_reachable retries: refused once, then connected, is True."""
		mock_connect.side_effect = [OSError("refused"), MagicMock()]
		with patch("src.rollout.inputs.time.sleep"):
			self.assertTrue(validation.tcp_reachable("10.0.0.1", 22))


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

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
	def test_valid_device_is_added(self, _):
		"""A valid, reachable row becomes one Device with no errors."""
		devices, errors = self.parser.prepare_devices([self._raw()])
		self.assertEqual(len(devices), 1)
		self.assertEqual(errors, [])
		self.assertIsInstance(devices[0], Device)

	@patch("src.rollout.inputs.tcp_reachable", return_value=False)
	def test_unreachable_device_excluded(self, _):
		"""An unreachable device is left out, with the error
		"10.0.0.1:22 is not reachable"."""
		devices, errors = self.parser.prepare_devices([self._raw()])
		self.assertEqual(len(devices), 0)
		self.assertEqual(errors, ["10.0.0.1:22 is not reachable"])

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
	def test_invalid_ip_excluded(self, _):
		"""A row with an invalid ip gives no device."""
		devices, _ = self.parser.prepare_devices([self._raw(ip="bad")])
		self.assertEqual(len(devices), 0)

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
	def test_device_type_lowercased(self, _):
		"""The device type is lowercased: CISCO_IOS becomes cisco_ios."""
		devices, _ = self.parser.prepare_devices([self._raw(device_type="CISCO_IOS")])
		self.assertEqual(devices[0].device_type, "cisco_ios")

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
	def test_blank_cells_are_empty_values_not_missing_columns(self, _):
		"""Blank secret and label cells are accepted: the secret stays "" and the
		label falls back to the ip, with no errors."""
		devices, errors = self.parser.prepare_devices(
			[self._raw(secret="", label="")])
		self.assertEqual(errors, [])
		self.assertEqual((devices[0].secret, devices[0].label), ("", "10.0.0.1"))

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
	def test_short_rows_from_csv_reader_are_tolerated(self, _):
		"""A None cell (DictReader gives None for missing trailing cells) still
		gives one device and no errors."""
		row = self._raw()
		row["secret"] = None
		devices, errors = self.parser.prepare_devices([row])
		self.assertEqual((len(devices), errors), (1, []))

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
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

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
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

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
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

	@patch("src.rollout.inputs.tcp_reachable", return_value=True)
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
