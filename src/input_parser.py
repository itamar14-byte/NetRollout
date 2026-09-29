import uuid
from csv import DictReader
from json import loads

from sqlalchemy.orm import Session

from src.db.tables import Inventory
from src.validation import Validator
from src.core import Device
from src.logging_utils import RolloutLogger


class InputParser:
	def __init__(self, validator: Validator, logger: RolloutLogger):
		self.validator = validator
		self.logger = logger

	CREDENTIAL_KEYS = ("username", "password", "secret")
	CORE_KEYS = {"ip", "device_type", "port", "label", *CREDENTIAL_KEYS}

	def prepare_devices(self, raw_devices: list[dict[str, str]],
	                    require_credentials: bool = True) -> tuple[
		list[Device], list[str]]:
		"""Validate raw rows (CSV / form) into Devices.
		Each row is handled independently — a bad row is reported in `errors`
		and never aborts the rest. Blank cells are treated as empty values
		(e.g. a blank enable `secret`), not as missing columns.
		:param require_credentials: the CLI pushes with the row's credentials,
		 so they're required there; inventory import doesn't store
		 credentials (they live in security profiles), so it passes False
		:return: (devices, errors)
		"""
		devices, errors = [], []
		for row_no, raw in enumerate(raw_devices, start=1):
			# DictReader yields None for missing trailing cells (and a None key
			# for surplus ones); normalise to stripped strings
			item = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
			item["device_type"] = item.get("device_type", "").lower()
			ip, port = item.get("ip", ""), item.get("port", "")
			if not ip or not port:
				errors.append(f"Row {row_no}: ip and port are required")
				continue
			if not self.validator.validate_device_data(item):
				errors.append(f"Row {row_no} ({ip}): invalid ip, port or "
				              f"device type")
				continue
			if require_credentials and not (item.get("username") and
			                                item.get("password")):
				errors.append(f"Row {row_no} ({ip}): username and password "
				              f"are required")
				continue
			if not self.validator.test_tcp_port(ip, int(port)):
				errors.append(f"{ip} is not reachable")
				self.logger.notify(f"{ip} is not reachable", "red")
				continue

			core = {k: item.get(k, "") for k in self.CORE_KEYS}
			core["label"] = core["label"] or ip
			core["port"] = int(port)
			extra = {k: v for k, v in item.items()
			         if k not in self.CORE_KEYS and v}
			if "vrfs" in extra:
				extra["vrfs"] = [vrf.strip() for vrf in extra["vrfs"].split(",")
				                 if vrf.strip()]
			devices.append(Device(**core, extra=extra))
			self.logger.notify(
				f"Device {item['device_type']}: {ip} successfully added", "green")
		return devices, errors

	@staticmethod
	def import_from_inventory(raw_devices: list[Inventory],
	                          user_id: uuid.UUID) -> list[Device]:
		return [Device.from_inventory(row, user_id) for row in raw_devices]

	def csv_to_inventory(self, device_path: str, user_id: uuid.UUID,
	                     db_session: Session, label: str = None) -> tuple[
		list[Device],list[str]]:
		device_path = device_path.strip('"')
		if self.validator.validate_file_extension(device_path, "csv"):
			try:
				# Reads devices CSV
				with open(device_path, "r", encoding="utf-8-sig") as file:
					required_keys = {
						"ip",
						"device_type",
						"port",
					}
					# Parses csv file into an iterable of dictionaries with the headers as keys
					reader = DictReader(file)

					# Check if all required fields are there
					missing_keys = required_keys - set(reader.fieldnames)
					if missing_keys:
						raise ValueError(
							"Missing keys: {}".format(missing_keys))

					devices, errors = self.prepare_devices(
						list(reader), require_credentials=False)
					self.logger.notify(
						f"CSV processed: {len(devices)} imported, {len(errors)} failed",
						"green" if not errors else "yellow", important=True)
					for device in devices:
						row = Inventory(user_id=user_id, ip=device.ip,
						                port=device.port,
						                device_type=device.device_type,
						                label=label or device.label)  # form > row > IP
						db_session.add(row)
					return devices,errors

			except FileNotFoundError:
				self.logger.notify(f"file not found", "red")
				return [],[]
			except PermissionError:
				self.logger.notify(f"can't access file", "red")
				return [],[]
			except Exception as e:
				self.logger.notify(f"Parsing failed: {e}", "red")
				return [],[]

		else:
			return [],[]

	def form_to_inventory(self, devices_json: str, user_id: uuid.UUID,
	                      db_session: Session) -> list[Device]:
		raw_devices = loads(devices_json) if devices_json else []
		devices, _ = self.prepare_devices(raw_devices=raw_devices,
		                                   require_credentials=False)
		# logs summary of file processing workflow
		#self.logger.notify(f"Devices loaded: {devices}","green")

		'''self.logger.notify(
			f"Devices file successfully processed\n"
			f" {len(devices)} devices found",
			"green")'''
		for device in devices:
			row = Inventory(user_id=user_id, ip=device.ip,
			                port=device.port,
			                device_type=device.device_type,
			                label=device.label if device.label else device.ip)
			db_session.add(row)
		# return the processed data
		return devices


	def parse_commands(self, commands_path: str) -> list[str]:
		commands_path = commands_path.strip('"')
		if self.validator.validate_file_extension(commands_path,"txt"):
			try:
				# Same rules as the web path: UTF-8 (utf-8-sig drops a BOM
				# that would otherwise stick to the first command), lines
				# stripped, blank lines dropped
				with open(commands_path, "r", encoding="utf-8-sig") as file:
					commands = [line for raw in file if (line := raw.strip())]
				# logs summary of file processing workflow
				self.logger.notify(
					f"Commands file successfully processed\n"
					f"{len(commands)} commands will be executed",
					"green")
				return commands
				# if an exception is thrown in parsing or validation fails, an error message is printed,
				# and the function returns an empty list

			except UnicodeDecodeError:
				self.logger.notify("commands file must be UTF-8 text", "red")
				return []

			except FileNotFoundError:
				self.logger.notify(f"file not found", "red")
				return []

			except PermissionError:
				self.logger.notify(f"can't access file", "red")
				return []

			except Exception as e:
				self.logger.notify(f"Parsing failed: {e}", "red")
				return []
		else:
			return []