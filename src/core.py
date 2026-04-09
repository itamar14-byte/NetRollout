import os
import threading
from dataclasses import dataclass, field
from typing import Optional
import encryption
import netmiko
import napalm

from logging_utils import RolloutLogger
from tables import Inventory


@dataclass (slots=True, kw_only=True)
class RolloutOptions:
	verify: bool = False
	verbose: bool = False
	webapp: bool = False


@dataclass (kw_only=True)
class Device:
	ip: str
	label: str
	username: str
	password: str = field(repr=False)
	device_type: str
	secret: str = field(repr=False)
	port: int



	def netmiko_connector(self) -> dict[str,str]:
		params = {
			"ip" : self.ip,
			"username" : self.username,
			"password" : self.password,
			"device_type" : self.device_type,
			"port" : self.port,
			"secret" : self.secret
		}
		return params

	def fetch_config(self, logger: RolloutLogger) -> Optional[str]:
		"""
		The function is tasked with connecting to a device and getting the running configuration, saved into a string,
		which will be searched downstream
		 In that case, notifications will be added to SSE _queue
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
			if netmiko_to_napalm.get(self.device_type):
				driver = napalm.get_network_driver(
					netmiko_to_napalm.get(self.device_type))
				node = driver(
					hostname=self.ip,
					username=self.username,
					password=self.password,
					optional_args={"secret": self.secret}
				)
				# Opens a connection to the device and saves the running config
				node.open()
				config = node.get_config()["running"]
				node.close()
				return config
			# If we encounter an issue in connection,
			# an error message is printed and logged, and we return false
			else:
				logger.notify(
					f"issue verifying {self.ip}: {self.device_type} is not supported for verification",
					"red")
				return None

		except Exception as e:
			logger.notify(f"could not connect to {self.ip}: {e}", "red")
			return None


	@classmethod
	def from_inventory(cls, row: Inventory) -> "Device":
		profile = row.security_profiles
		if not profile:
			raise ValueError(f"no security profiles assigned to {row.ip}")
		return cls(ip=row.ip,label=row.label,device_type=row.device_type,
		           port=row.port,username=encryption.decrypt(
				profile.username),password=encryption.decrypt(
				profile.password_secret),secret=encryption.decrypt(
				profile.enable_secret) if profile.enable_secret else "")


class RolloutEngine:
	def __init__(self, param: RolloutOptions, devices: list[Device],
	             commands: list[str])-> None:
		self._verify_flag = param.verify
		self._devices = devices
		self._commands = commands

	def _push_config(self, cancel_event: threading.Event,
	                 logger: RolloutLogger) -> str | None:
		"""
		The function will accept device and command data, as processed by parse_files and push the configuration,
		using netmiko for SSH connections over the provided ip and port
		:return: the function does not return anything, but executes the _commands
		"""
		# Goes over the dictionary list, each time focusing on a single device
		for device in self._devices:
			if cancel_event and cancel_event.is_set():
				logger.notify("Rollout Canceled By User", color="red")
				return "cancel_sent"
			else:
				logger.notify(
					f"connecting to {device.ip}:{device.port}",
					"yellow")

				# Tests tcp connectivity to the device on the requested port
				try:
					# Initialise a netmiko connection object
					net_connect = (netmiko.ConnectHandler
					               (**(device.netmiko_connector())))
					logger.notify(
						f"{device.ip} connected successfully",
						"green")
					# Goes into privileged config mode, depending on the platform
					net_connect.enable()
					net_connect.config_mode()

					# Runs all _commands in order,
					# and checks that the command was accepted in the device
					# In case of syntax error or rejection, an error message is printed,
					# and we move to the next command
					for command in self._commands:
						output = net_connect.send_config_set(
							[command.strip()], exit_config_mode=False
						)
						errors = ["Invalid", "unrecognized", "unknown"]
						if any(err.lower() in output.lower() for err in errors):
							logger.notify(
								f"{command} failed on {device.ip}: {output}",
								"red")
							continue

					# After _commands finish running,
					# the configuration is saved and we gracefully close the SSH session
					net_connect.exit_config_mode()
					net_connect.save_config()
					net_connect.disconnect()

				# In case of exception or issue in connecting and executing the _commands,
				# an error message will be printed, and we move to the next device
				except netmiko.NetMikoAuthenticationException:
					logger.notify(f"{device.ip} authentication failed", "red")
					continue
				except netmiko.NetmikoTimeoutException:
					logger.notify(f"{device.ip} timed out", "red")
					continue
				except Exception as e:
					logger.notify(f"{device.ip} failed: {e}", "red")
					continue
		return None

	def _verify(self, cancel_event: threading.Event,
	                 logger: RolloutLogger) -> dict[str, int] | str:
		"""
		The function gets the list of _devices and verifies which _devices have been successfully configured
		by comparing the _commands to the config file from fetch_config()
		:return: returns a counter of successful matches
		"""
		result = {}
		# Loops through the _devices and gets the running config, using fetch config function
		for device in self._devices:
			if cancel_event and cancel_event.is_set():
				logger.notify("Rollout Canceled By User", color="red")
				return "cancel_sent"

			successful_commands = 0
			config = device.fetch_config(logger)
			# If there is a config file,
			# we go through the command list
			# and check it against the running config string
			if config:
				rejects = []
				for command in self._commands:
					command = command.strip()
					# If a command has no match in the config, we print a notification. On a successful match,
					# we increment the counter
					if command.lower() not in config.lower():
						rejects.append(command)
						logger.notify(
							f"{command} not configured on {device.ip}",
							"red")
					else:
						successful_commands += 1
				# when a device has no rejects, such that all _commands match, we increment the counter, self.notify the user and
				# move to the next device
				if not rejects:
					logger.notify(
						f"{device.ip} successfully configured",
						"green")
				# Updates the result dictionary with the device ip and the number of successful _commands
				result.update({device.ip: successful_commands})
		return result


	def run(self, cancel_flag: threading.Event, logger:RolloutLogger) -> int:
		logger.notify("Starting configuration rollout")
		# Runs parse_files to get data from the provided file paths
		# If parsing was successful and the output of the function was not empty lists, we continue the process
		if self._devices and self._commands:
			# Runs the config push procedure
			push = self._push_config(cancel_flag, logger)
			if push == "cancel_sent":
				return 1

			# If the _verify flag is activated, runs the _verify function,
			# getting a dictionary of the _devices and the successful _commands count
			if self._verify_flag:
				logger.notify(
					"Configuration rollout finished. Initiating verification process"
				)
				device_count = self._verify(cancel_flag, logger)
				if device_count == "cancel_sent":
					return 1
				failed, partial, successful = 0, 0, 0

				# Number of successful _commands in each device and status of
				# _devices,
				# based on comparing the value to the list of _commands
				for node in device_count.items():
					if node[1] == 0:
						failed += 1
					elif 0 < node[1] < len(self._commands):
						partial += 1
					else:
						successful += 1

					logger.notify(
						f"{node[0]} successfully configured with"
						f" {node[1]}/{len(self._commands)} _commands")

				# Logs and prints (if _verbose), the rollout status per device and the summary
				logger.notify(f"{failed} _devices failed rollout", "red")
				logger.notify(
					f"{partial} _devices with problems in configuration",
					"yellow")
				logger.notify(f"{successful} _devices successfully configured",
				       "green")
				logger.notify(f"Please see Execution logs in "
				              f"{os.path.abspath(logger.logfile)}")
				return 0

			logger.notify(
				f"Configuration rollout complete. "
				f"{len(self._devices)} _devices configured",
				"green")
			logger.notify(f"Please see Execution logs in {os.path.abspath
			(logger.logfile)}")
			return 0

		else:
			logger.notify(f"Device input invalid", "red")
			return 1