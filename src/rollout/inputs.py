"""What a rollout is given, checked: an IP / port / platform's validity,
an address's standard spelling, whether a device answers on its port, and
reading a devices CSV (the CLI's, and the web app's inventory import - one
format for both) and a commands file into Devices. Writing an import into
the inventory is src/inventory.py's (import_csv)."""
from __future__ import annotations   # type hints are never evaluated

import ipaddress
import os
import re
import socket
import time
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from src.rollout.engine import Device, endpoint
from src.rollout.log import RolloutLogger, Tone
from src.rollout.platforms import PLATFORMS
if TYPE_CHECKING:   # annotations only: the DB models load the web stack, which the CLI (.exe) must not
	from src.db.tables import Inventory


# Netmiko device types NetRollout supports: those it knows how to finish a
# push on (src/rollout/platforms.py)
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


def normalize_ip(ip: str) -> str:
	"""An address in its one standard spelling: an IPv6 address has many
	(2001:DB8:0:0:0:0:0:1, 2001:0db8::0001, ...), and NetRollout compares
	addresses as text - duplicates, rollback matching, labels, the
	reachability cache. IPv4 has one already.

	:returns: lower case, zeros compressed (2001:db8::1)
	:raises ValueError: not an address (validate_ip first)"""
	return str(ipaddress.ip_address(ip.strip()))


def validate_port(port: str) -> bool:
	""":returns: whether `port` (as typed) is a TCP port number, 1-65535"""
	# isascii: isnumeric() / isdigit() also accept "²", "½", "①" - int() doesn't
	if not (port.isascii() and port.isdigit()):
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
		# 10056 on Windows; create_connection picks IPv4 or IPv6
		try:
			with socket.create_connection((ip, port), timeout=TCP_TIMEOUT):
				return True
		except OSError:
			if attempt < TCP_RETRIES - 1:
				time.sleep(TCP_RETRY_DELAY)
	return False


def command_lines(text: str) -> list[str]:
	"""The commands of a commands text - a commands file, the pasted box, one
	platform's box: one per line, stripped, blank lines dropped.

	:returns: the commands, in order"""
	return [line for raw in text.splitlines() if (line := raw.strip())]


def read_commands(data: bytes) -> list[str]:
	"""The commands of a commands file's bytes (the CLI's file, the web
	upload): UTF-8 - utf-8-sig, so the BOM Windows Notepad writes doesn't
	stick to the first command -, then command_lines.

	:raises UnicodeDecodeError: not UTF-8 text"""
	return command_lines(data.decode("utf-8-sig"))


def token_problem(token: str) -> str | None:
	"""A variable mapping's token, without its $$ marks.

	:returns: what's wrong with it (for the page), None when it's valid"""
	if token.strip():
		if re.match(r'^[A-Za-z0-9_]+$', token):
			if len(token) <= 64:
				return None
			return "Token must be maximum 64 characters long"
		return "Token must contain only letters, numbers and underscores"
	return "Token cannot be empty"


def property_name_problem(property_name: str, allowed: set[str]) -> str | None:
	""":param allowed: the user's property names — system defaults plus
	 their own definitions (webapp: InventoryView.property_defs)
	:returns: what's wrong with it (for the page), None when it's valid"""
	if property_name.strip().lower() not in allowed:
		return f"Property name {property_name} is not valid"
	return None


def index_problem(index: int | None, property_name: str,
                  list_properties: set[str]) -> str | None:
	"""Only list properties (system `vrfs`, or user-defined lists) can be
	indexed.

	:param index: the position in the list; None: the whole value
	:returns: what's wrong with it (for the page), None when it's valid"""
	if index is None:
		return None
	if property_name not in list_properties:
		return f"Property {property_name} can not be indexed"
	if index < 0:
		return "Index cannot be negative"
	return None


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
			self._logger.notify(f"{path} is not a file", Tone.ERROR)
			return False
		if not path.lower().endswith(extension):
			self._logger.notify(f"file must be {extension}", Tone.ERROR)
			return False
		return True

	def validate_device_data(self, device: dict[str, str]) -> bool:
		"""One row of a devices CSV: a valid IP, port and supported platform;
		the first problem is reported."""
		if not validate_ip(device["ip"]):
			self._logger.notify(f"{device['ip']} is not a valid IP address",
			                    Tone.ERROR)
		elif not validate_port(device["port"]):
			self._logger.notify(f"{device['port']} is not a valid port number",
			                    Tone.ERROR)
		elif not validate_platform(device["device_type"]):
			self._logger.notify(f"{device['device_type']} is not supported",
			                    Tone.ERROR)
		else:
			return True
		return False


class InputParser:
	"""Reads devices and commands for a rollout or an import."""

	def __init__(self, validator: Validator, logger: RolloutLogger):
		""":param validator: checks the files and each row
		:param logger: where each problem and the summary are reported"""
		self.validator = validator
		self.logger = logger

	CREDENTIAL_KEYS = ("username", "password", "secret")
	CORE_KEYS = {"ip", "device_type", "port", "label", *CREDENTIAL_KEYS}

	def prepare_devices(self, raw_devices: list[dict[str, str]],
	                    require_credentials: bool = True,
	                    check_reachable: bool = True) -> tuple[
		list[Device], list[str]]:
		"""Validate raw rows (CSV / form) into Devices.
		Each row is handled independently — a bad row is reported in `errors`
		and never aborts the rest. Blank cells are treated as empty values
		(e.g. a blank enable `secret`), not as missing columns.
		One CSV format serves both consumers; the flags pick the rules:
		:param require_credentials: the CLI pushes with the row's credentials,
		 so they're required there; inventory import turns them into security
		 profiles when present, so it passes False
		:param check_reachable: the CLI is about to push, so it fails early on
		 an unreachable device; inventory is a catalog (reachability is shown
		 live), so import passes False
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
			ip = normalize_ip(ip)
			if require_credentials and not (item.get("username") and
			                                item.get("password")):
				errors.append(f"Row {row_no} ({ip}): username and password "
				              f"are required")
				continue
			if check_reachable and not tcp_reachable(ip, int(port)):
				# returned like every other row error: the caller logs them
				errors.append(f"{endpoint(ip, port)} is not reachable")
				continue

			extra: dict[str, str | list[str]] = {
				k: v for k, v in item.items() if k not in self.CORE_KEYS and v}
			if "vrfs" in extra:
				extra["vrfs"] = [vrf.strip() for vrf in item["vrfs"].split(",")
				                 if vrf.strip()]
			devices.append(Device(
				ip=ip, label=item.get("label") or ip, port=int(port),
				device_type=item["device_type"],
				username=item.get("username", ""), password=item.get("password", ""),
				secret=item.get("secret", ""), extra=extra))
			self.logger.notify(
				f"Device {item['device_type']}: {ip} successfully added", Tone.SUCCESS)
		return devices, errors

	@staticmethod
	def import_from_inventory(raw_devices: list[Inventory], user_id: uuid.UUID,
	                          attributes: Mapping[uuid.UUID, dict[str, Any]] | None = None
	                          ) -> list[Device]:
		"""Inventory rows made Devices for a rollout (their profiles decrypted).

		:param user_id: who rolls out - only their own variable mappings apply
		:param attributes: each device's attribute values as that user sees
		 them, by device id (the web app: InventoryView.attributes); None:
		 the rows' own (var_maps)
		:raises ValueError: a device without a security profile"""
		return [Device.from_inventory(
			row, user_id, None if attributes is None else attributes.get(row.id, {}))
			for row in raw_devices]

	def parse_commands(self, commands_path: str) -> list[str]:
		"""The commands of a commands file (read_commands: the web upload's
		rules).

		:returns: the commands; [] when the file is missing or unreadable (the
		 reason is logged)"""
		commands_path = commands_path.strip('"')
		if self.validator.validate_file_extension(commands_path,"txt"):
			try:
				with open(commands_path, "rb") as file:
					commands = read_commands(file.read())
				self.logger.notify(
					f"Commands file successfully processed\n"
					f"{len(commands)} commands will be executed",
					Tone.SUCCESS)
				return commands

			except UnicodeDecodeError:
				self.logger.notify("commands file must be UTF-8 text", Tone.ERROR)
				return []

			except FileNotFoundError:
				self.logger.notify("file not found", Tone.ERROR)
				return []

			except PermissionError:
				self.logger.notify("can't access file", Tone.ERROR)
				return []

			except Exception as e:
				self.logger.notify(f"Parsing failed: {e}", Tone.ERROR)
				return []
		else:
			return []
