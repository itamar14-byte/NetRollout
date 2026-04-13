import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional, TypedDict

import napalm
import netmiko

import encryption
from logging_utils import RolloutLogger
from tables import Inventory


class DeviceResultDict(TypedDict):
    device_ip: str
    device_type: str
    commands_sent: int
    commands_verified: int | None
    status: str


@dataclass(slots=True, kw_only=True)
class RolloutOptions:
    verify: bool = False
    verbose: bool = False
    webapp: bool = False
    max_workers: int = 10


@dataclass(kw_only=True)
class Device:
    ip: str
    label: str
    username: str
    password: str = field(repr=False)
    device_type: str
    secret: str = field(repr=False)
    port: int
    var_map_subs: dict[str, tuple[str | list[str], str  | None]]  = field(
        default_factory=dict)
    extra: dict = field(default_factory=dict)

    def netmiko_connector(self) -> dict[str, str]:
        params = {
            "ip": self.ip,
            "username": self.username,
            "password": self.password,
            "device_type": self.device_type,
            "port": self.port,
            "secret": self.secret
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
        profile = row.security_profile
        mappings = row.var_mappings
        if not profile:
            raise ValueError(f"no security profiles assigned to {row.ip}")

        parsed_mappings = {m.token: (m.property_name, m.index) for m in mappings}

        return cls(ip=row.ip, label=row.label, device_type=row.device_type,
                   port=row.port, username=profile.username,
                   password=encryption.decrypt(profile.password_secret),
                   secret=encryption.decrypt(profile.enable_secret) if
                   profile.enable_secret else "",
                   var_map_subs=parsed_mappings,
                   extra=row.var_maps or {})


class RolloutEngine:
    def __init__(self, param: RolloutOptions, devices: list[Device],
                 commands: list[str]) -> None:
        self.devices = devices
        self._verify_flag = param.verify
        self._max_workers = param.max_workers
        self._commands = commands

    def _substitute_commands(self, device: Device) -> list[str]:
        device_mappings = device.var_map_subs
        commands_copy = self._commands.copy()
        for token, (property_name, index) in device_mappings.items():
            property_value = device.extra[property_name]
            if index is not None:
                property_value = property_value[index]

            property_value = str(property_value).strip()
            commands_copy = [command.replace(token, property_value)
                             for command in commands_copy]
        return commands_copy



    def _push_device(self, device: Device, cancel_event: threading.Event,
                     logger: RolloutLogger) -> tuple[str, bool | None]:
        """
        Pushes configuration to a single device via Netmiko SSH.
        Called concurrently by _push_config via ThreadPoolExecutor.
        :return: (ip, True) on success, (ip, False) on failure,
                 (ip, None) if cancelled before connecting
        """
        if cancel_event and cancel_event.is_set():
            return device.ip, None

        logger.notify(f"connecting to {device.ip}:{device.port}", "yellow")
        commands_sent = False
        try:
            # Initialise a netmiko connection object
            net_connect = netmiko.ConnectHandler(**(device.netmiko_connector()))
            logger.notify(f"{device.ip} connected successfully", "green")
            # Goes into privileged config mode, depending on the platform
            net_connect.enable()
            net_connect.config_mode()

            # Runs all _commands in order,
            # and checks that the command was accepted in the device
            # In case of syntax error or rejection, an error message is printed,
            # and we move to the next command
            for command in self._substitute_commands(device):
                commands_sent = True
                output = net_connect.send_config_set(
                    [command.strip()], exit_config_mode=False)
                errors = ["Invalid", "unrecognized", "unknown"]
                if any(err.lower() in output.lower() for err in errors):
                    logger.notify(
                        f"{command} failed on {device.ip}: {output}", "red")
                    continue

            # After _commands finish running,
            # the configuration is saved and we gracefully close the SSH session
            net_connect.exit_config_mode()
            net_connect.save_config()
            net_connect.disconnect()
            return device.ip, True

        # In case of exception or issue in connecting and executing the _commands,
        # an error message will be printed, and we move to the next device
        except netmiko.NetMikoAuthenticationException:
            logger.notify(f"{device.ip} authentication failed", "red")
            return device.ip, False
        except netmiko.NetmikoTimeoutException:
            logger.notify(f"{device.ip} timed out", "red")
            return device.ip, False
        except netmiko.exceptions.ReadTimeout as e:
            if commands_sent:
                # Prompt changed mid-session (e.g. hostname rename) — config was applied
                logger.notify(f"{device.ip}: prompt detection lost after config push"
                              f" — treating as success", "yellow")
                return device.ip, True
            logger.notify(f"{device.ip} failed: {e}", "red")
            return device.ip, False
        except Exception as e:
            logger.notify(f"{device.ip} failed: {e}", "red")
            return device.ip, False

    def _push_config(self, cancel_event: threading.Event,
                     logger: RolloutLogger) -> tuple[str | None, dict[str, bool]]:
        """
        The function will accept device and command data, as processed by parse_files and push the configuration,
        using netmiko for SSH connections over the provided ip and port.
        Devices are pushed concurrently via ThreadPoolExecutor.
        :return: (cancel_signal, push_results) where cancel_signal is "cancel_sent" or None
        """
        push_results = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {
                executor.submit(self._push_device, device, cancel_event, logger): device
                for device in self.devices
            }
            for future in as_completed(futures):
                ip, result = future.result()
                if result is None:
                    logger.notify("Rollout Canceled By User", color="red")
                    return "cancel_sent", push_results
                push_results[ip] = result
        return None, push_results



    def _verify_device(self, device: Device,
                       logger: RolloutLogger) -> tuple[str, int]:
        """
        Verifies a single device by fetching its running config and comparing
        against the substituted commands. Called concurrently by _verify.
        :return: (ip, successful_commands_count)
        """
        successful_commands = 0
        # Loops through the devices and gets the running config, using fetch config function
        config = device.fetch_config(logger)
        # If there is a config file,
        # we go through the command list
        # and check it against the running config string
        if config:
            rejects = []
            for command in self._substitute_commands(device):
                command = command.strip()
                # If a command has no match in the config, we print a notification. On a successful match,
                # we increment the counter
                if command.lower() not in config.lower():
                    rejects.append(command)
                    logger.notify(
                        f"{command} not configured on {device.ip}", "red")
                else:
                    successful_commands += 1
            # when a device has no rejects, such that all _commands match, we increment the counter, self.notify the user and
            # move to the next device
            if not rejects:
                logger.notify(f"{device.ip} successfully configured", "green")
        # Updates the result dictionary with the device ip and the number of successful _commands
        return device.ip, successful_commands

    def _verify(self, logger: RolloutLogger) -> dict[str, int]:
        """
        The function gets the list of devices and verifies which devices have been successfully configured
        by comparing the _commands to the config file from fetch_config()
        Devices are verified concurrently via ThreadPoolExecutor.
        :return: returns a dict of {ip: successful_commands_count}
        """
        result = {}
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = {
                executor.submit(self._verify_device, device, logger): device
                for device in self.devices
            }
            for future in as_completed(futures):
                ip, count = future.result()
                result[ip] = count
        return result

    def run(self, cancel_flag: threading.Event, logger: RolloutLogger) -> list[DeviceResultDict]:
        logger.notify("Starting configuration rollout", important=True)
        # Runs parse_files to get_queue data from the provided file paths
        # If parsing was successful and the output of the function was not empty lists, we continue the process
        if self.devices and self._commands:
            # Runs the config push procedure
            cancel_signal, push_results = self._push_config(cancel_flag, logger)

            # If the _verify flag is activated, runs the _verify function,
            # getting a dictionary of the devices and the successful _commands count
            verify_results = {}
            if self._verify_flag and cancel_signal != "cancel_sent":
                logger.notify(
                    "Configuration rollout finished. Initiating verification process",
                    important=True
                )
                verify_results = self._verify(logger)
                failed, partial, successful = 0, 0, 0

                # Number of successful _commands in each device and status of
                # devices,
                # based on comparing the value to the list of _commands
                for node in verify_results.items():
                    if node[1] == 0:
                        failed += 1
                    elif 0 < node[1] < len(self._commands):
                        partial += 1
                    else:
                        successful += 1

                    logger.notify(
                        f"{node[0]} successfully configured with"
                        f" {node[1]}/{len(self._commands)} commands",
                        important=True)

                # Logs and prints (if _verbose), the rollout status per device and the summary
                if failed > 0:
                    logger.notify(f"{failed} devices failed rollout", "red")
                if partial > 0:
                    logger.notify(
                        f"{partial} devices with problems in configuration",
                        "yellow", important=True)
                logger.notify(f"{successful} devices successfully configured",
                              "green", important=True)

            logger.notify(
                f"Configuration rollout complete. "
                f"{len(self.devices)} devices configured",
                "green", important=True)
            logger.notify(
                f"Please see Execution logs in {os.path.abspath(logger.logfile)}",
                important=True)

            results = []
            for device in self.devices:
                if device.ip not in push_results:
                    status, commands_sent, commands_verified = "cancelled", 0, None
                elif not push_results[device.ip]:
                    status, commands_sent, commands_verified = "failed", 0, None
                else:
                    commands_sent = len(self._commands)
                    verified_count = verify_results.get(device.ip, None)
                    if verified_count is None:
                        status, commands_verified = "success", None
                    elif verified_count == commands_sent:
                        status, commands_verified = "success", verified_count
                    elif verified_count > 0:
                        status, commands_verified = "partial", verified_count
                    else:
                        status, commands_verified = "failed", 0
                results.append(DeviceResultDict(device_ip=device.ip,
                                              device_type=device.device_type,
                                              commands_sent=commands_sent,
                                              commands_verified=commands_verified,
                                              status=status))
            return results

        else:
            logger.notify(f"Device input invalid", "red")
            return []
