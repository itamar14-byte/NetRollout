"""Input checks.

Pure functions — an IP, a port, a platform, a variable mapping's fields —
used alike by the web routes and the CLI; tcp_reachable() probes a device's
management port; Validator checks the files a rollout is read from (devices
CSV, commands file) and reports each problem to its RolloutLogger."""
import ipaddress
import os
import re
import socket
import time

from src.logging_utils import RolloutLogger
from src.platforms import PLATFORMS

# Netmiko device types NetRollout supports: those it knows how to finish a
# push on (src/platforms.py)
SUPPORTED_PLATFORMS = frozenset(PLATFORMS)

TCP_TIMEOUT = 5
TCP_RETRIES = 3
TCP_RETRY_DELAY = 1


def validate_ip(ip: str) -> bool:
	""":returns: whether `ip` is an IPv4 or IPv6 address"""
	try:
		ipaddress.ip_address(ip)
		return True
	except ValueError:
		return False


def validate_port(port: str) -> bool:
	""":returns: whether `port` (as typed) is a TCP port number, 1-65535"""
	if not port.isnumeric():
		return False
	return 1 <= int(port) <= 65535


def validate_platform(platform: str) -> bool:
	""":returns: whether NetRollout supports this Netmiko device type"""
	return platform in SUPPORTED_PLATFORMS


def tcp_reachable(ip: str, port: int = 22) -> bool:
	"""Can the device be reached on its management (SSH) port? TCP_RETRIES
	attempts, TCP_RETRY_DELAY seconds apart, TCP_TIMEOUT each.

	:returns: whether one of the attempts connected"""
	for attempt in range(TCP_RETRIES):
		# a fresh socket per attempt — reusing a failed one raises WinError
		# 10056 on Windows
		try:
			with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
				conn.settimeout(TCP_TIMEOUT)
				conn.connect((ip, port))
				return True
		except OSError:
			if attempt < TCP_RETRIES - 1:
				time.sleep(TCP_RETRY_DELAY)
	return False


def validate_var_map_inner_token(token: str) -> tuple[bool, str | None]:
	"""A variable mapping's token, without its $$ marks.

	:returns: (valid, why not - for the page)"""
	if token.strip():
		if re.match(r'^[A-Za-z0-9_]+$', token):
			if len(token) <= 64:
				return True, None
			return False, "Token must be maximum 64 characters long"
		return False, "Token must contain only letters, numbers and underscores"
	return False, "Token cannot be empty"


def validate_var_map_property_name(property_name: str, allowed: set[str]) \
		-> tuple[bool, str | None]:
	""":param allowed: the user's property names — system defaults plus
	 their own definitions (webapp: get_property_defs)
	:returns: (valid, why not - for the page)"""
	if property_name.strip().lower() not in allowed:
		return False, f"Property name {property_name} is not valid"
	return True, None


def validate_var_index(index: int | None, property_name: str,
                       list_properties: set[str]) -> tuple[bool, str | None]:
	"""Only list properties (system `vrfs`, or user-defined lists) can be
	indexed.

	:param index: the position in the list; None: the whole value
	:returns: (valid, why not - for the page)"""
	if index is None:
		return True, None
	if property_name not in list_properties:
		return False, f"Property {property_name} can not be indexed"
	if index < 0:
		return False, "Index cannot be negative"
	return True, None


class Validator:
	"""Checks the files a rollout is read from, reporting each problem to
	the logger (the CLI's console, an import's log)."""

	def __init__(self, logger: RolloutLogger):
		""":param logger: where problems are reported"""
		self._logger = logger

	def validate_file_extension(self, path: str, extension: str) -> bool:
		"""The path is an existing file with the expected extension (csv for
		devices, txt for commands)."""
		if not os.path.isfile(path):
			self._logger.notify(f"{path} is not a file", "red")
			return False
		if not path.lower().endswith(extension):
			self._logger.notify(f"file must be {extension}", "red")
			return False
		return True

	def validate_device_data(self, device: dict[str, str]) -> bool:
		"""One row of a devices CSV: a valid IP, port and supported platform;
		the first problem is reported."""
		if not validate_ip(device["ip"]):
			self._logger.notify(f"{device['ip']} is not a valid IP address",
			                    "red")
		elif not validate_port(device["port"]):
			self._logger.notify(f"{device['port']} is not a valid port number",
			                    "red")
		elif not validate_platform(device["device_type"]):
			self._logger.notify(f"{device['device_type']} is not supported",
			                    "red")
		else:
			return True
		return False
