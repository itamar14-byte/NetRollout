import uuid
from csv import DictReader
from json import loads

from sqlalchemy.orm import Session

from tables import Inventory
from validation import Validator
from core import Device
from logging_utils import base_notify


class InputParser:
	def __init__(self, validator: Validator):
		self.validator = validator

	def _prepare_devices(self, raw_devices: list[dict[str, str]],
	                    verbose: bool = False,
	                    webapp: bool = False) -> list[Device]:
		"""Helper function for the file parser that processes the device dictionary
		 :param raw_devices: preprocessed device list
		 :param verbose: boolean flags
		 stating whether the user wishes to see progress messages on the console
		 :param webapp: boolean flag indicating the requester is the GUI app
		 :return: a list of dictionaries with fields and values for the devices.
		 In case of failure, an empty list
		"""
		# process all validated devices into a list of dictionaries
		devices = []
		for item in raw_devices:
			item["device_type"] = item["device_type"].lower()
			if item["ip"] and self.validator.validate_device_data(item,
			                                                      webapp=webapp):
				if self.validator.test_tcp_port(item["ip"], int(item["port"])):
					item.setdefault("label", item["ip"])
					devices.append(Device(**item))
					base_notify(
						f"Device {item['device_type']}: {item['ip']} successfully added",
						"green",
						verbose, webapp=webapp
					)
				else:
					base_notify(f"{item['ip']} is not reachable", "red",
					            webapp=webapp)
					continue
			else:
				continue
		return devices

	@staticmethod
	def import_from_inventory(devices: list[Inventory], commands: list[
		str]) -> tuple[list[Device],list[str]]:
		return [Device.from_inventory(row) for row in devices], commands

	def csv_to_inventory(self, device_path: str, user_id: uuid.UUID,
	                     db_session: Session, label: str = None) -> list[Device]:
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

					devices = self._prepare_devices(list(reader))
					for device in devices:
						row = Inventory(user_id=user_id, ip=device.ip,
						                port=device.port,
						                device_type=device.device_type,
						                label=label if label else device.ip)
						db_session.add(row)
					return devices

			except FileNotFoundError:
				base_notify(f"file not found", "red")
				return []
			except PermissionError:
				base_notify(f"can't access file", "red")
				return []
			except Exception as e:
				base_notify(f"Parsing failed: {e}", "red")
				return []

		else:
			return []

	def form_to_inventory(self, devices_json: str, user_id: uuid.UUID,
	                      db_session: Session) -> list[Device]:
		raw_devices = loads(devices_json) if devices_json else []
		devices = self._prepare_devices(raw_devices=raw_devices, webapp=True)
		# logs summary of file processing workflow
		base_notify(f"Devices loaded: {devices}", webapp=True, verbose=False)

		base_notify(
			f"Devices file successfully processed\n"
			f" {len(devices)} devices found",
			"green",
			webapp=True,
		)
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
				with open(commands_path, "r") as file:
					commands = file.readlines()
					# logs summary of file processing workflow
					base_notify(
						f"Devices file successfully processed\n"
						f"{len(commands)} commands will be executed",
						"green",
					)
				return commands
				# if an exception is thrown in parsing or validation fails, an error message is printed,
				# and the function returns a tuple of empty lists

			except FileNotFoundError:
				base_notify(f"file not found", "red")
				return []

			except PermissionError:
				base_notify(f"can't access file", "red")
				return []

			except Exception as e:
				base_notify(f"Parsing failed: {e}", "red")
				return []
		else:
			return []

