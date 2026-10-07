"""What a rollout is given, checked: an IP / port / platform's validity,
an address's standard spelling, whether a device answers on its port, and
reading a devices CSV (the CLI's, and the web app's inventory import - one
format for both) and a commands file into Devices. The import's helpers turn
attribute columns into a device's variables and credential columns into
security profiles."""
from __future__ import annotations   # type hints are never evaluated

import datetime
import hmac
import ipaddress
import os
import re
import socket
import time
import uuid
from collections import Counter
from csv import DictReader
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.encryption import decrypt, encrypt
from src.rollout.engine import Device, endpoint
from src.rollout.log import RolloutLogger
from src.rollout.platforms import PLATFORMS
if TYPE_CHECKING:
	# The web app's import uses the DB models; the CLI (.exe) never does, so
	# they're imported where used — loading them pulls in the web stack
	from sqlalchemy.orm import Session
	from src.db.tables import Inventory, SecurityProfile


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
		# 10056 on Windows; create_connection picks IPv4 or IPv6
		try:
			with socket.create_connection((ip, port), timeout=TCP_TIMEOUT):
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
				f"Device {item['device_type']}: {ip} successfully added", "green")
		return devices, errors

	@staticmethod
	def import_from_inventory(raw_devices: list[Inventory],
	                          user_id: uuid.UUID) -> list[Device]:
		"""Inventory rows made Devices for a rollout (their profiles decrypted).

		:param user_id: who rolls out - only their own variable mappings apply
		:raises ValueError: a device without a security profile"""
		return [Device.from_inventory(row, user_id) for row in raw_devices]

	def csv_to_inventory(self, device_path: str, user_id: uuid.UUID,
	                     db_session: Session, label: str | None = None,
	                     properties: list[dict[str, Any]] | None = None,
	                     create_profiles: bool = False) -> "ImportReport":
		"""Import a devices CSV into the user's inventory (web app).
		The same CSV the CLI takes: core columns make the device, attribute
		columns that name one of the user's properties (system or custom, by
		name or label) become its variable attributes, and credential columns
		become security profiles when `create_profiles` is set. Other columns
		are reported, never dropped silently. No reachability check.
		:param device_path: the uploaded CSV
		:param user_id: whose inventory it goes into
		:param db_session: the rows are added to it; the caller commits
		:param label: applied to every device (form label > row label > IP)
		:param properties: the user's property definitions
		 ({name, label, is_list}) — see WebServices.get_property_defs
		:param create_profiles: credential columns become security profiles
		:returns: what was imported, the rows' errors and notices for the page"""
		from src.db.tables import Inventory   # web app only
		report = ImportReport()
		device_path = device_path.strip('"')
		if not self.validator.validate_file_extension(device_path, "csv"):
			report.errors.append("File must be a .csv")
			return report
		try:
			with open(device_path, "r", encoding="utf-8-sig") as file:
				reader = DictReader(file)
				headers = [h.strip() for h in (reader.fieldnames or []) if h]
				missing = [k for k in ("ip", "device_type", "port")
				           if k not in headers]
				if missing:
					report.errors.append(
						f"Missing required columns: {', '.join(missing)}")
					return report
				rows = list(reader)
		except UnicodeDecodeError:
			report.errors.append("The CSV must be UTF-8 text")
			return report
		except OSError as e:
			self.logger.notify(f"Reading CSV failed: {e}", "red")
			report.errors.append("The CSV file could not be read")
			return report

		columns = _classify_columns(headers, properties or [])
		if columns.ignored:
			report.notices.append(("warning",
				f"Ignored columns: {', '.join(columns.ignored)} — not a property; "
				f"create it under Properties first"))
		if columns.credentials and not create_profiles:
			report.notices.append(("info",
				"Credential columns were not imported (profile creation was "
				"off) — assign a security profile to the devices"))

		devices, report.errors = self.prepare_devices(
			rows, require_credentials=False, check_reachable=False)
		profiles = (_ProfileResolver(user_id, db_session)
		            if create_profiles and columns.credentials else None)
		half_credentials: list[str] = []
		for device in devices:
			row = Inventory(user_id=user_id, ip=device.ip, port=device.port,
			                device_type=device.device_type,
			                label=label or device.label,  # form > row > IP
			                var_maps=_var_maps(device.extra, columns) or None)
			if profiles:
				if device.username and device.password:
					row.security_profile = profiles.resolve(
						device.username, device.password, device.secret)
				elif device.username or device.password:
					half_credentials.append(row.label)
			db_session.add(row)
		if half_credentials:
			shown = ", ".join(half_credentials[:5]) + (
				", …" if len(half_credentials) > 5 else "")
			report.notices.append(("warning",
				f"{len(half_credentials)} device"
				f"{'s' if len(half_credentials) != 1 else ''} imported without "
				f"a profile (username and password are both needed): {shown}"))
		if profiles:
			db_session.flush()  # ids for the audit trail
			report.created_profiles = profiles.created()
			report.notices.extend(profiles.summary())
		report.devices = devices
		self.logger.notify(
			f"CSV processed: {len(devices)} imported, "
			f"{len(report.errors)} failed",
			"green" if not report.errors else "yellow", important=True)
		return report

	def parse_commands(self, commands_path: str) -> list[str]:
		"""The commands of a commands file: UTF-8, one per line, stripped,
		blank lines dropped.

		:returns: the commands; [] when the file is missing or unreadable (the
		 reason is logged)"""
		commands_path = commands_path.strip('"')
		if self.validator.validate_file_extension(commands_path,"txt"):
			try:
				# Same rules as the web path: UTF-8 (utf-8-sig drops a BOM
				# that would otherwise stick to the first command), lines
				# stripped, blank lines dropped
				with open(commands_path, "r", encoding="utf-8-sig") as file:
					commands = [line for raw in file if (line := raw.strip())]
				self.logger.notify(
					f"Commands file successfully processed\n"
					f"{len(commands)} commands will be executed",
					"green")
				return commands

			except UnicodeDecodeError:
				self.logger.notify("commands file must be UTF-8 text", "red")
				return []

			except FileNotFoundError:
				self.logger.notify("file not found", "red")
				return []

			except PermissionError:
				self.logger.notify("can't access file", "red")
				return []

			except Exception as e:
				self.logger.notify(f"Parsing failed: {e}", "red")
				return []
		else:
			return []


# ── CSV import helpers (web app) ─────────────────────────────────────────────

@dataclass
class ImportReport:
	"""Outcome of a CSV import. `notices` are (category, message) pairs for
	the UI: "success" / "info" / "warning"."""
	devices: list[Device] = field(default_factory=list)
	errors: list[str] = field(default_factory=list)
	notices: list[tuple[str, str]] = field(default_factory=list)
	created_profiles: list[tuple[uuid.UUID, str]] = field(default_factory=list)


@dataclass
class _Columns:
	"""An import CSV's columns, sorted by what they become."""
	props: dict[str, dict[str, Any]]   # CSV header → property definition
	credentials: list[str]   # credential headers present
	ignored: list[str]       # headers that are neither


def _classify_columns(headers: list[str], properties: list[dict[str, Any]]) -> _Columns:
	"""Match headers to properties by name or label, case-insensitively,
	treating spaces and underscores alike ("Loopback IP" → loopback_ip)."""
	def norm(name: str) -> str:
		return name.strip().lower().replace(" ", "_")
	lookup: dict[str, dict[str, Any]] = {}
	for prop in properties:
		lookup.setdefault(norm(prop["name"]), prop)
		if prop.get("label"):
			lookup.setdefault(norm(prop["label"]), prop)
	core = InputParser.CORE_KEYS - set(InputParser.CREDENTIAL_KEYS)
	columns = _Columns(props={}, credentials=[], ignored=[])
	for header in headers:
		if header in core:
			continue
		if header in InputParser.CREDENTIAL_KEYS:
			columns.credentials.append(header)
		elif norm(header) in lookup:
			columns.props[header] = lookup[norm(header)]
		else:
			columns.ignored.append(header)
	return columns


def _var_maps(extra: dict[str, str | list[str]], columns: _Columns) -> dict[str, str | list[str]]:
	"""A row's attribute cells → var_maps, with the edit modal's rules:
	list properties split on commas, empty values skipped."""
	var_maps: dict[str, str | list[str]] = {}
	for header, prop in columns.props.items():
		value = extra.get(header)
		if not value:
			continue
		if prop.get("is_list"):
			parts = value if isinstance(value, list) else value.split(",")
			items = [v.strip() for v in parts if v.strip()]
			if items:
				var_maps[prop["name"]] = items
		else:
			var_maps[prop["name"]] = value if isinstance(value, str) \
				else ", ".join(value)
	return var_maps


class _ProfileResolver:
	"""Credential columns → security profiles, for one import.
	A row reuses the user's profile with exactly the same username, password
	and enable secret; otherwise a new profile is created with a unique label
	and later rows with the same credentials share it. Existing profiles are
	never modified. Matching decrypts the user's own profiles in memory only —
	the same exposure as a rollout; nothing is logged or stored in clear."""

	def __init__(self, user_id: uuid.UUID, db_session: Session):
		from src.db.tables import SecurityProfile   # web app only
		self._user_id, self._db = user_id, db_session
		self._known: list[tuple[tuple[str, str, str], SecurityProfile]] = []
		for p in db_session.query(SecurityProfile).filter_by(user_id=user_id):
			secret = decrypt(p.enable_secret) if p.enable_secret else ""
			self._known.append(
				((p.username, decrypt(p.password_secret), secret), p))
		self._labels = {p.label for _, p in self._known if p.label}
		self._created: list[SecurityProfile] = []
		self._assigned: Counter[SecurityProfile] = Counter()
		self._warnings: list[str] = []

	def resolve(self, username: str, password: str,
	            secret: str) -> SecurityProfile:
		""":returns: the user's profile with exactly these credentials - one
		 that existed, one this import created, or a new one"""
		creds = (username, password, secret or "")
		profile = next((p for known, p in self._known
		                if _same_credentials(known, creds)), None)
		if profile is None:
			profile = self._create(creds)
		self._assigned[profile] += 1
		return profile

	def created(self) -> list[tuple[uuid.UUID, str]]:
		"""(id, label) of the profiles this import created — after flush."""
		return [(p.id, _name(p)) for p in self._created]   # always labelled

	def summary(self) -> list[tuple[str, str]]:
		""":returns: (category, message) per profile used, then the warnings -
		 for the page's notices"""
		lines = []
		for profile, count in self._assigned.items():
			devices = f"{count} device{'s' if count != 1 else ''}"
			if profile in self._created:
				lines.append(("success", f"{devices} assigned to new profile "
				                         f"'{_name(profile)}'"))
			else:
				lines.append(("info", f"{devices} assigned to existing profile "
				                      f"'{_name(profile)}'"))
		return lines + [("warning", w) for w in self._warnings]

	def _create(self, creds: tuple[str, str, str]) -> SecurityProfile:
		"""A new profile for these credentials (encrypted), with a warning when
		another of the user's profiles has the same username."""
		from src.db.tables import SecurityProfile   # web app only
		username, password, secret = creds
		label = self._unique_label(username)
		profile = SecurityProfile(
			label=label, username=username,
			password_secret=encrypt(password),
			enable_secret=encrypt(secret) if secret else None,
			user_id=self._user_id)
		self._db.add(profile)
		# Same username, different credentials: a typo or an old password in
		# the CSV is the likely cause, so say so instead of hiding it
		for (other_user, other_pw, _), other in self._known:
			if other_user == username:
				what = ("password" if not hmac.compare_digest(
					other_pw.encode(), password.encode()) else "enable secret")
				self._warnings.append(
					f"Created profile '{label}' — your profile "
					f"'{_name(other)}' also uses username {username}, with a "
					f"different {what}. If the CSV has a typo or an old "
					f"password, fix the row or move the devices to "
					f"'{_name(other)}'.")
				break
		self._known.append((creds, profile))
		self._labels.add(label)
		self._created.append(profile)
		return profile

	def _unique_label(self, username: str) -> str:
		""":returns: "<username> · CSV import 7 Oct", numbered when taken"""
		today = datetime.date.today()
		base = f"{username[:36]} · CSV import {today.day} {today:%b}"
		label, n = base, 2
		while label in self._labels:
			label, n = f"{base} ({n})", n + 1
		return label


def _same_credentials(a: tuple[str, str, str], b: tuple[str, str, str]) -> bool:
	"""Equal credentials - every field compared (no early exit), each in
	constant time: how long it takes says nothing about the secrets."""
	return all([hmac.compare_digest(x.encode(), y.encode())
	            for x, y in zip(a, b)])


def _name(profile: SecurityProfile) -> str:
	""":returns: how the page names a profile (its label, else its username)"""
	return profile.label or profile.username
