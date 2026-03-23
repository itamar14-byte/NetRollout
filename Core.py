from csv import DictReader
from threading import Event
from typing import Optional

from netmiko import *
from napalm import get_network_driver

from validation import (test_tcp_port, validate_file_extension,
                        validate_device_data)
from logging_utils import notify, BASEDIR, LOGFILE


def prepare_devices(raw_devices: list[dict[str, str]],
                    verbose: bool,
                    webapp: bool = False,
                    cancel_event:Optional[Event] = None) \
		-> list[dict[str, str]]:
	"""Helper function for the file parser that processes the device dictionary
		 :param raw_devices: preprocessed device list
		 :param verbose: boolean flags
		 stating whether the user wishes to see progress messages on the console
		 :param webapp: boolean flag indicating the requester is the GUI app
		 :param cancel_event: threading.Event instance indicating a cancel
		 request from user
		 :return: a list of dictionaries with fields and values for the devices.
		 In case of failure, an empty list
		"""
	# process all validated devices into a list of dictionaries
	devices = []
	for item in raw_devices:
		if cancel_event and cancel_event.is_set():
			notify("Rollout Canceled By User", color="red", webapp=webapp)
			return []

		item["device_type"] = item["device_type"].lower()
		if item["ip"] and validate_device_data(item,webapp=webapp):
			if test_tcp_port(item["ip"], int(item["port"])):
				devices.append(item)
				notify(
					f"Device {item['device_type']}: {item['ip']} successfully added",
					"green",
					verbose,webapp=webapp
				)
			else:
				notify(f"{item['ip']} is not reachable", "red",
				       webapp=webapp)
				continue
		else:
			continue
	return devices


def parse_files(
		device_path: str, commands_path: str, verbose: bool = False
) -> tuple[list[dict[str, str]], list[str]]:
	"""This function accepts paths to a csv file detailing devices
	 (using the fields ip,user,password, platform, secret, port)
	as well as a txt file with a configuration file needed to push. The function then parses the files
	 into objects that can be further processed.
	 :param device_path: A file path to a csv of network devices
	 :param commands_path: A file path to a txt file with commands
	 :param verbose: boolean flags stating whether the user wishes to see progress messages on the console
	 :return: a list of dictionaries with fields and values for the devices and a list of commands. In case of failure,
	 a tuple of empty lists
	"""

	# normalize Windows file paths
	device_path = device_path.strip('"')
	commands_path = commands_path.strip('"')

	# Check file names are valid and exist
	if validate_file_extension(commands_path,
	                           "txt") and validate_file_extension(
		device_path, "csv"
	):

		try:
			# Reads devices CSV
			with open(device_path, "r", encoding="utf-8-sig") as file:

				required_keys = {
					"ip",
					"username",
					"password",
					"device_type",
					"secret",
					"port",
				}
				# Parses csv file into an iterable of dictionaries with the headers as keys
				reader = DictReader(file)

				# Check if all required fields are there
				missing_keys = required_keys - set(reader.fieldnames)
				if missing_keys:
					raise ValueError("Missing keys: {}".format(missing_keys))

				devices = prepare_devices(list(reader), verbose)
			# Parses command file directly into a list where each element is a command
			with open(commands_path, "r") as file:
				commands = file.readlines()
				# logs summary of file processing workflow
				notify(
					f"Devices file successfully processed\n"
					f" {len(devices)} devices found\n"
					f"{len(commands)} commands will be executed",
					"green",
				)
			# return the processed data
			return devices, commands

		# if an exception is thrown in parsing or validation fails, an error message is printed,
		# and the function returns a tuple of empty lists

		except FileNotFoundError:
			notify(f"file not found", "red")
			return [], []

		except PermissionError:
			notify(f"can't access file", "red")
			return [], []

		except Exception as e:
			notify(f"Parsing failed: {e}", "red")
			return [], []
	else:
		return [], []


def push_config(
		devices: list[dict[str, str]],
		commands: list[str],
		verbose: bool = False,
		webapp: bool = False,
		cancel_event: Event = None,
) -> str | None:
	"""
	The function will accept device and command data, as processed by parse_files and push the configuration,
	using netmiko for SSH connections over the provided ip and port

	:param cancel_event: Threading object that allows to check if the cancel event is set
	:param webapp: boolean value stating weather the function was called as part of a web deployment.
	 In that case, notifications will be added to SSE queue
	:param devices: list of dictionaries with device data
	:param commands: lists of the commands to be executed, in order
	:param verbose: a boolean flag determining weather logs would be
	displayed in console
	:return: the function does not return anything, but executes the commands
	"""
	# Goes over the dictionary list, each time focusing on a single device
	for device in devices:
		if cancel_event and cancel_event.is_set():
			notify("Rollout Canceled By User", color="red", webapp=webapp)
			return "cancel_sent"
		else:
			notify(
				f"connecting to {device['ip']}:{device['port']}",
				"yellow",
				verbose,
				webapp=webapp,
			)

			# Tests tcp connectivity to the device on the requested port
			try:
				# Initialise a netmiko connection object
				net_connect = ConnectHandler(**device)
				notify(
					f"{device['ip']} connected successfully",
					"green",
					verbose,
					webapp=webapp,
				)
				# Goes into privileged config mode, depending on the platform
				net_connect.enable()
				net_connect.config_mode()

				# Runs all commands in order,
				# and checks that the command was accepted in the device
				# In case of syntax error or rejection, an error message is printed,
				# and we move to the next command
				for command in commands:
					output = net_connect.send_config_set(
						[command.strip()], exit_config_mode=False
					)
					errors = ["Invalid", "unrecognized", "unknown"]
					if any(err.lower() in output.lower() for err in errors):
						notify(
							f"{command} failed on {device['ip']}: {output}",
							"red",
							verbose,
							webapp=webapp,
						)
						continue

				# After commands finish running,
				# the configuration is saved and we gracefully close the SSH session
				net_connect.exit_config_mode()
				net_connect.save_config()
				net_connect.disconnect()

			# In case of exception or issue in connecting and executing the commands,
			# an error message will be printed, and we move to the next device
			except NetMikoAuthenticationException:
				notify(f"{device['ip']} authentication failed", "red",
				       webapp=webapp)
				continue
			except NetmikoTimeoutException:
				notify(f"{device['ip']} timed out", "red", webapp=webapp)
				continue
			except Exception as e:
				notify(f"{device['ip']} failed: {e}", "red", webapp=webapp)
				continue
	return None


def fetch_config(device: dict[str, str], webapp: bool = False) -> Optional[str]:
	"""
	The function is tasked with connecting to a device and getting the running configuration, saved into a string,
	which will be searched downstream
	:param webapp: boolean value stating weather the function was called as part of a web deployment.
	 In that case, notifications will be added to SSE queue
	:param device: a dictionary with device dataset
	:return: if connection is successful, the function returns the running config as a string and returns false otherwise
	"""
	# Translation dictionary mapping Netmiko device type values to corresponding NAPALM values
	netmiko_to_napalm = {
		"fortinet": "fortios",
		"paloalto_panos": "panos",
		"cisco_ios": "ios",
		"cisco_nxos": "nxos",
		"cisco_xe": "iosxe",
		"cisco_xr": "iosxr",
		"juniper_junos": "junos",
		"arista_eos": "eos",
		"aruba_aoscx": "aoscx",
		"checkpoint_gaia": False,
		"hp_procurve": "procurve",
		"hp_comware": False,
	}

	try:
		# Attempts to establish a NAPALM connection to the device, using the translated platform value
		# and other device fields, contingent on if the platform has a supported NAPALM driver
		if netmiko_to_napalm.get(device["device_type"]):
			driver = get_network_driver(
				netmiko_to_napalm.get(device["device_type"]))
			node = driver(
				hostname=device["ip"],
				username=device["username"],
				password=device["password"],
				optional_args={"secret": device["secret"]},
			)
			# Opens a connection to the device and saves the running config
			node.open()
			config = node.get_config()["running"]
			node.close()
			return config
		# If we encounter an issue in connection,
		# an error message is printed and logged, and we return false
		else:
			notify(
				f"issue verifying {device['ip']}: {device['device_type']} is not supported for verification",
				"red",
				webapp=webapp,
			)
			return None

	except Exception as e:
		notify(f"could not connect to {device['ip']}: {e}", "red",
		       webapp=webapp)
		return None


def verify(
		devices: list[dict[str, str]],
		commands: list[str],
		verbose: bool = False,
		webapp: bool = False,
		cancel_event: Event = None,
) -> dict[str, int] | str:
	"""
	The function gets the list of devices and verifies which devices have been successfully configured
	by comparing the commands to the config file from fetch_config()

	:param cancel_event: Threading object that allows to check if the cancel event is set
	:param webapp: boolean value stating weather the function was called as part of a web deployment.
	 In that case, notifications will be added to SSE queue
	:param devices: a dictionary dataset with a device information
	:param commands: list of expected commands
	:param verbose: a boolean flag determining weather logs would be displayed in console
	:return: returns a counter of successful matches
	"""
	result = {}
	# Loops through the devices and gets the running config, using fetch config function
	for device in devices:
		if cancel_event and cancel_event.is_set():
			notify("Rollout Canceled By User", color="red", webapp=webapp)
			return "cancel_sent"

		successful_commands = 0
		config = fetch_config(device, webapp=webapp)
		# If there is a config file,
		# we go through the command list
		# and check it against the running config string
		if config:
			rejects = []
			for command in commands:
				command = command.strip()
				# If a command has no match in the config, we print a notification. On a successful match,
				# we increment the counter
				if command not in config:
					rejects.append(command)
					notify(
						f"{command} not configured on {device['ip']}",
						"red",
						verbose,
						webapp=webapp,
					)
				else:
					successful_commands += 1
			# when a device has no rejects, such that all commands match, we increment the counter, notify the user and
			# move to the next device
			if not rejects:
				notify(
					f"{device['ip']} successfully configured",
					"green",
					verbose,
					webapp=webapp,
				)
			# Updates the result dictionary with the device ip and the number of successful commands
			result.update({device["ip"]: successful_commands})
	return result


def rollout_runner(devices: list[dict[str, str]],
                   commands: list[str],
                   verify_rollout: bool = False,
                   verbose: bool = False,
                   webapp: bool = False,
                   cancel_event:Optional[Event] = None) -> int:
	notify("Starting configuration rollout")
	# Runs parse_files to get data from the provided file paths
	try:
		# If parsing was successful and the output of the function was not empty lists, we continue the process
		if devices and commands:

			# Runs the config push procedure
			push = push_config(devices=devices,
			            commands=commands,
			            verbose=verbose,
			            webapp=webapp,
			            cancel_event=cancel_event)
			if push == "cancel_sent":
				return 1

			# If the verify flag is activated, runs the verify function,
			# getting a dictionary of the devices and the successful commands count
			if verify_rollout:
				notify(
					"Configuration rollout finished. Initiating verification process"
				)
				device_count = verify(devices=devices,
				                      commands=commands,
				                      verbose=verbose,
				                      webapp=webapp,
				                      cancel_event=cancel_event)
				if device_count == "cancel_sent":
					return 1
				failed, partial, successful = 0, 0, 0

				# Number of successful commands in each device and status of
				# devices,
				# based on comparing the value to the list of commands
				for node in device_count.items():
					if node[1] == 0:
						failed += 1
					elif 0 < node[1] < len(commands):
						partial += 1
					else:
						successful += 1

					notify(
						f"{node[0]} successfully configured with {node[1]}/{len(commands)} commands",
						"green",
						verbose, webapp=webapp
					)

				# Logs and prints (if verbose), the rollout status per device and the summary
				notify(f"{failed} devices failed rollout", "red",
				       webapp=webapp)
				notify(
					f"{partial} devices with problems in configuration",
					"yellow", webapp=webapp
				)
				notify(f"{successful} devices successfully configured",
				       "green", webapp=webapp)
				notify(f"Please see Execution logs in {BASEDIR}\\{LOGFILE}",
				       webapp=webapp)

				return 0

			notify(
				f"Configuration rollout complete. {len(devices)} devices configured",
				"green", webapp=webapp
			)
			notify(f"Please see Execution logs in {BASEDIR}\\{LOGFILE}",
			       webapp=webapp)

			return 0

		else:
			notify(f"Device file invalid", "red",webapp=webapp)
			return 1

	except ValueError as e:
		notify(f"Device file invalid: {e}", "red",webapp=webapp)
		return 1