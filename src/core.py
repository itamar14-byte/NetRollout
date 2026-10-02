import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import NamedTuple, Optional, TypedDict

import netmiko

from src import encryption
from src.logging_utils import RolloutLogger
from src.db.tables import Inventory


class SubstitutionError(ValueError):
	"""A $$TOKEN$$ can't be resolved on a device."""
	pass


def mapping_resolvable(var_maps: dict | None, property_name: str,
                       index: int | None) -> bool:
	"""Whether a mapping can substitute on a device: the attribute is set and
	non-empty, and for indexed mappings (e.g. vrfs[2]) the list is long
	enough. Shared by the binding routes and the engine, so they can't
	disagree."""
	value = (var_maps or {}).get(property_name)
	if not value:
		return False
	if index is None:
		return True
	return isinstance(value, list) and 0 <= index < len(value)


@dataclass(frozen=True)
class Platform:
	"""How a platform finishes a push and prints its config in the syntax
	engineers type.
	finish: "save" → save_config() after leaving config mode; "commit" →
	 commit() before leaving it (leaving discards uncommitted changes); any
	 other text → that CLI command after leaving config mode; "" → nothing
	 (the change is live as typed)."""
	finish: str
	show_config: tuple[str, ...] = ("show running-config",)
	flat: bool = False         # one "set …" line per setting, no sections
	discard: str = ""          # after a failed commit: drop the candidate
	close_blocks: bool = False  # FortiOS: an open config block is discarded


PLATFORMS = {
	"cisco_ios": Platform("save"),
	"cisco_xe": Platform("save"),
	"cisco_nxos": Platform("save"),
	"cisco_xr": Platform("commit"),        # a failed commit reverts itself
	"arista_eos": Platform("save"),
	"aruba_aoscx": Platform("save"),
	"hp_procurve": Platform("save"),
	"hp_comware": Platform("save", ("display current-configuration",)),
	"juniper_junos": Platform("commit", ("show configuration | display set",),
	                          flat=True, discard="rollback 0"),
	"paloalto_panos": Platform("commit", ("set cli config-output-format set",
	                                      "show config running"), flat=True),
	"checkpoint_gaia": Platform("save config", ("show configuration",),
	                            flat=True),
	"fortinet": Platform("", ("show",), close_blocks=True),
}

# PAN-OS commits can take minutes; Netmiko waits 120 s by default
COMMIT_TIMEOUT = 300

# A device's reply to a command it refused (any vendor; matched with the
# command's own echo removed, so "description unknown-host" isn't one)
REJECTION_MARKERS = ("% invalid", "invalid input", "invalid command",
                     "invalid syntax", "unknown command", "unrecognized command",
                     "syntax error", "command fail", "% incomplete",
                     "% ambiguous", "error:")
# Leave or close a config section — they configure nothing
NAVIGATION = {"exit", "end", "next", "abort", "quit", "return", "top", "up",
              "root"}   # IOS-XR: back to the top of config mode


def _navigates(word: str) -> bool:
	# also IOS exit-address-family, exit-vrf, …
	return word in NAVIGATION or word.startswith("exit-")

# Operational commands: they leave no trace in the config to check
NOT_VERIFIABLE = ("commit", "write", "copy ", "clear ", "do ", "show ", "save",
                  "reload", "ping ", "traceroute ", "request ", "run ")

# Per-command verify verdicts
VERIFIED, NOT_CONFIGURED, STILL_CONFIGURED = "verified", "not configured", \
	"still configured"
UNVERIFIABLE, VARIABLE = "not verifiable", "variable"


def rejection(output: str, command: str) -> str | None:
	"""The device's complaint about `command`, or None if it was accepted.
	The echo of the command itself is skipped, so its own words (e.g.
	"description invalid-vlan") are never mistaken for a complaint."""
	for line in output.splitlines():
		text = line.strip()
		# On the echo line only the prompt before the command is the device's
		# ("Invalid input: foo" for the command "foo" is still a complaint)
		said = text[:-len(command)] if command and text.endswith(command) \
			else text
		if any(marker in said.lower() for marker in REJECTION_MARKERS):
			return text
	return None


def _norm(line: str) -> str:
	# Spacing, case and FortiOS / ProCurve quoting don't change the meaning
	return " ".join(line.replace('"', "").split()).lower()


class _Section:
	__slots__ = ("children",)

	def __init__(self):
		self.children: dict[str, _Section] = {}


def _parse_config(config: str) -> _Section:
	"""The config as a tree of sections by indentation. Comment and separator
	lines ('!', '#') are skipped; at column 0 they also end any open section
	(Comware indents its top-level lines after '#')."""
	root = _Section()
	stack: list[tuple[int, _Section]] = [(-1, root)]
	for line in config.splitlines():
		stripped = line.strip()
		if not stripped:
			continue
		indent = len(line) - len(line.lstrip())
		if stripped[0] in "!#":
			if indent == 0:
				stack = [(-1, root)]
			continue
		while stack[-1][0] >= indent:
			stack.pop()
		node = stack[-1][1].children.setdefault(_norm(stripped), _Section())
		stack.append((indent, node))
	return root


def _verify_sectioned(config: str, commands: list[str],
                      blocks: bool = False) -> list[str]:
	"""Verdicts for an indented config (Cisco-style, FortiOS). Typed commands
	are flat — the device tracks the mode — so each is placed in its section
	using the config's own structure: a typed line that is a section there
	opens it; exit/next close one; a command found only at an outer level
	leaves the section (as IOS does); one found nowhere stays where it was.
	blocks (FortiOS): "end" closes only the current config block — elsewhere
	it leaves configuration altogether."""
	root = _parse_config(config)
	stack: list[_Section] = []
	verdicts = []
	for command in commands:
		key = _norm(command)
		word = key.split()[0] if key else ""
		if _navigates(word):
			leaves_all = word in ("return", "root") or (word == "end" and not blocks)
			stack = [] if leaves_all else stack[:-1]
			verdicts.append(UNVERIFIABLE)
			continue
		if key.startswith(NOT_VERIFIABLE):
			verdicts.append(UNVERIFIABLE)
			continue
		# A removal: "no X" (Cisco-style), "undo X" (Comware), "unset X" /
		# "delete X" (FortiOS)
		removed = None
		if word in ("no", "undo", "unset", "delete"):
			removed = key.split(" ", 1)[1] if " " in key else ""
		forms = [key] if removed is None else [key, removed]
		found, depth = None, len(stack)
		for level in range(len(stack), -1, -1):
			node = stack[level - 1] if level else root
			found = next((node.children[f] for f in forms
			              if f in node.children), None)
			if found is not None:
				depth = level
				break
		stack = stack[:depth]
		here = stack[-1] if stack else root
		if removed is None:
			verdicts.append(VERIFIED if found is not None else NOT_CONFIGURED)
			if found is not None and found.children:
				stack.append(found)
		else:
			gone = not any(child == removed or child.startswith(removed + " ")
			               or child in (f"set {removed}", f"edit {removed}")
			               or child.startswith(f"set {removed} ")
			               for child in here.children)
			verdicts.append(VERIFIED if gone else STILL_CONFIGURED)
	return verdicts


def _verify_flat(config: str, commands: list[str]) -> list[str]:
	"""Verdicts for a flat config (Junos display set, PAN-OS set format,
	Gaia): one full line per setting. "edit" / "up" / "top" move the prefix
	that relative "set" / "delete" commands extend."""
	lines = {_norm(line) for line in config.splitlines() if line.strip()}
	prefix: list[list[str]] = []
	verdicts = []
	for command in commands:
		words = _norm(command).split()
		word = words[0] if words else ""
		if word == "edit":
			prefix.append(words[1:])
			verdicts.append(UNVERIFIABLE)
		elif word in ("top", "end"):
			prefix = []
			verdicts.append(UNVERIFIABLE)
		elif _navigates(word):
			prefix = prefix[:-1]
			verdicts.append(UNVERIFIABLE)
		elif " ".join(words).startswith(NOT_VERIFIABLE):
			verdicts.append(UNVERIFIABLE)
		else:
			path = [w for level in prefix for w in level]
			if word in ("set", "delete"):
				words = [word, *path, *words[1:]]
			if word == "delete":
				# gone as a "set" line, and as an "add" line (Gaia's users etc.)
				targets = [" ".join([verb, *words[1:]]) for verb in ("set", "add")]
				gone = not any(line == t or line.startswith(t + " ")
				               for line in lines for t in targets)
				verdicts.append(VERIFIED if gone else STILL_CONFIGURED)
			else:
				verdicts.append(VERIFIED if " ".join(words) in lines
				                else NOT_CONFIGURED)
	return verdicts


def verify_commands(device_type: str, config: str,
                    commands: list[str]) -> list[str]:
	"""One verdict per command: verified / not configured / still configured
	(a removal that didn't take) / not verifiable (navigation, operational) /
	variable (an unresolved $$TOKEN$$ — the rollout log has the real one)."""
	platform = PLATFORMS[device_type]
	verdicts = _verify_flat(config, commands) if platform.flat else \
		_verify_sectioned(config, commands, blocks=platform.close_blocks)
	return [VARIABLE if "$$" in command else verdict
	        for command, verdict in zip(commands, verdicts)]


class PushResult(NamedTuple):
	applied: bool    # the change took effect (connected, finished, committed)
	rejected: int    # commands the device refused


class VerifyResult(NamedTuple):
	verified: int        # commands confirmed in the config
	checkable: int       # commands that can be checked (not navigation /
	                     # operational)
	config: str | None   # kept only when something didn't verify


class DeviceResultDict(TypedDict):
	device_ip: str
	device_port: int
	device_type: str
	commands_sent: int
	commands_verified: int | None
	fetched_config: str | None
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
	var_map_subs: dict[str, tuple[str | list[str], str | None]] = field(
		default_factory=dict)
	extra: dict = field(default_factory=dict)

	@property
	def endpoint(self) -> str:
		"""ip:port — what identifies a reachable target (the IP alone doesn't:
		NAT / port forwarding put several devices behind one address)."""
		return f"{self.ip}:{self.port}"

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
		"""The running config, printed in the syntax engineers type (see
		PLATFORMS), over the same SSH as the push — the device's port and
		credentials. None if it can't be fetched (reported in the log)."""
		platform = PLATFORMS[self.device_type]
		try:
			with netmiko.ConnectHandler(**self.netmiko_connector()) as conn:
				conn.enable()
				output = ""
				for command in platform.show_config:
					output = conn.send_command(command, read_timeout=60)
				return output
		except Exception as e:
			logger.notify(f"could not fetch the config of {self.endpoint} to "
			              f"verify it: {e}", "red")
			return None

	@classmethod
	def from_inventory(cls, row: Inventory, user_id) -> "Device":
		profile = row.security_profile
		# The join table is shared across users (global devices), so only the
		# rolling-out user's own mappings are applied
		mappings = [m for m in row.var_mappings if m.user_id == user_id]
		if not profile:
			raise ValueError(f"no security profiles assigned to {row.ip}")

		parsed_mappings = {m.token: (m.property_name, m.index) for m in
		                   mappings}

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
		""":raises SubstitutionError: a mapped attribute is missing on the
		 device (e.g. removed from a global device after users bound it)"""
		device_mappings = device.var_map_subs
		commands_copy = self._commands.copy()
		for token, (property_name, index) in device_mappings.items():
			if not mapping_resolvable(device.extra, property_name, index):
				where = property_name if index is None else \
					f"{property_name}[{index}]"
				raise SubstitutionError(
					f"{token}: device has no value for '{where}'")
			property_value = device.extra[property_name]
			if index is not None:
				property_value = property_value[index]

			property_value = str(property_value).strip()
			commands_copy = [command.replace(token, property_value)
			                 for command in commands_copy]
		return commands_copy

	def _push_device(self, device: Device, cancel_event: threading.Event,
	                 logger: RolloutLogger) -> tuple[str, "PushResult | None"]:
		"""
		Pushes configuration to a single device via Netmiko SSH, then finishes
		it the way its platform needs (PLATFORMS: save, commit, a command or
		nothing). Called concurrently by _push_config via ThreadPoolExecutor.
		A command the device refuses is reported with its reply and the rest
		are still sent.
		:return: (ip, PushResult) — applied False when nothing took effect
		 (no connection, a failed commit) — or (ip, None) if cancelled before
		 connecting
		"""
		if cancel_event and cancel_event.is_set():
			return device.ip, None

		# Resolve $$TOKEN$$s before opening SSH: a device whose mappings can't
		# resolve fails on its own with a clear reason, and is never touched
		try:
			commands = self._substitute_commands(device)
		except SubstitutionError as e:
			logger.notify(f"{device.endpoint} skipped — {e}", "red")
			return device.ip, PushResult(applied=False, rejected=0)

		platform = PLATFORMS[device.device_type]
		logger.notify(f"connecting to {device.ip}:{device.port}", "yellow")
		commands_sent = False
		try:
			net_connect = netmiko.ConnectHandler(**(device.netmiko_connector()))
			try:
				logger.notify(f"{device.ip} connected successfully", "green")
				# Goes into privileged config mode, depending on the platform
				net_connect.enable()
				net_connect.config_mode()

				rejected = 0
				for command in commands:
					commands_sent = True
					output = net_connect.send_config_set(
						[command.strip()], exit_config_mode=False)
					if complaint := rejection(output, command.strip()):
						rejected += 1
						logger.notify(f"{device.endpoint}: '{command.strip()}' "
						              f"rejected — {complaint}", "red")

				applied = self._finish(net_connect, platform, device, logger)
				return device.ip, PushResult(applied=applied, rejected=rejected)
			finally:
				# Always closed, also when the push fails half-way
				try:
					net_connect.disconnect()
				except Exception:
					pass

		# In case of exception or issue in connecting and executing the _commands,
		# an error message will be printed, and we move to the next device
		except netmiko.NetMikoAuthenticationException:
			logger.notify(f"{device.ip} authentication failed", "red")
			return device.ip, PushResult(applied=False, rejected=0)
		except netmiko.NetmikoTimeoutException:
			logger.notify(f"{device.ip} timed out", "red")
			return device.ip, PushResult(applied=False, rejected=0)
		except netmiko.exceptions.ReadTimeout as e:
			if commands_sent and platform.finish != "commit":
				# Prompt changed mid-session (e.g. hostname rename): the
				# commands were applied, but the finish couldn't run
				logger.notify(
					f"{device.endpoint}: prompt detection lost after the config "
					f"push — applied{', but NOT saved: save it on the device' if platform.finish else ''}",
					"yellow")
				return device.ip, PushResult(applied=True, rejected=0)
			logger.notify(f"{device.ip} failed: {e}", "red")
			return device.ip, PushResult(applied=False, rejected=0)
		except Exception as e:
			logger.notify(f"{device.ip} failed: {e}", "red")
			return device.ip, PushResult(applied=False, rejected=0)

	@staticmethod
	def _finish(conn, platform: Platform, device: Device,
	            logger: RolloutLogger) -> bool:
		""":return: False if the change didn't take effect (a failed commit)"""
		if platform.close_blocks:
			# FortiOS applies a block at "end"; one left open is discarded
			for _ in range(10):
				if "(" not in conn.find_prompt():
					break
				conn.send_command_timing("end")
		if platform.finish == "commit":
			# Before leaving config mode: leaving discards uncommitted changes
			try:
				conn.commit(read_timeout=COMMIT_TIMEOUT)
			except netmiko.exceptions.ReadTimeout:
				# It may still complete on the device: don't claim either way
				logger.notify(f"{device.endpoint}: the commit didn't finish within "
				              f"{COMMIT_TIMEOUT}s — check the device; reported as "
				              f"failed", "red")
				return False
			except ValueError as e:
				logger.notify(f"{device.endpoint}: commit failed — nothing was "
				              f"applied. {e}", "red")
				if platform.discard:
					conn.send_config_set([platform.discard],
					                     exit_config_mode=False)
				else:
					logger.notify(f"{device.endpoint}: the uncommitted changes "
					              f"are still in the candidate configuration — "
					              f"discard them on the device", "yellow")
				conn.exit_config_mode()
				return False
		conn.exit_config_mode()
		if platform.finish == "save":
			conn.save_config()
		elif platform.finish and platform.finish != "commit":
			conn.send_command(platform.finish)
		return True

	def _push_config(self, cancel_event: threading.Event,
	                 logger: RolloutLogger) -> tuple[
		str | None, dict[int, "PushResult"]]:
		"""
		The function will accept device and command data, as processed by parse_files and push the configuration,
		using netmiko for SSH connections over the provided ip and port.
		Devices are pushed concurrently via ThreadPoolExecutor.
		A cancel stops devices that haven't connected yet; devices already
		mid-push finish their commands (a half-applied config is worse than
		either state). Every result is collected — returning at the first
		cancelled device used to drop the results of devices still in flight,
		recording them as cancelled although their config was applied.
		:return: (cancel_signal, push_results) where cancel_signal is "cancel_sent" or None
		 and push_results maps each device's index in self.devices to its
		 result; devices that never connected are absent
		"""
		# Keyed by position, not IP: several devices can share an IP (NAT /
		# port forwarding, overlapping address space) and must not overwrite
		# each other's result
		push_results = {}
		cancelled = False
		with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
			futures = {
				executor.submit(self._push_device, device, cancel_event,
				                logger): idx
				for idx, device in enumerate(self.devices)
			}
			for future in as_completed(futures):
				_, result = future.result()
				if result is None:
					if not cancelled:
						logger.notify("Rollout Canceled By User", color="red")
					cancelled = True
					continue
				push_results[futures[future]] = result
		return ("cancel_sent" if cancelled else None), push_results

	def _verify_device(self, device: Device,
	                   logger: RolloutLogger) -> "VerifyResult | None":
		"""Fetches the device's config and checks every command in it
		(verify_commands). Called concurrently by _verify.
		:return: the counts, the config kept only when something didn't
		 verify (for Verify Diff) — or None if the config couldn't be fetched
		"""
		try:
			expected = self._substitute_commands(device)
		except SubstitutionError:
			return None   # already reported (and the device failed) at the push
		config = device.fetch_config(logger)
		if config is None:
			return None
		verdicts = verify_commands(device.device_type, config, expected)
		for command, verdict in zip(expected, verdicts):
			if verdict in (NOT_CONFIGURED, STILL_CONFIGURED):
				logger.notify(f"{device.endpoint}: '{command.strip()}' "
				              f"{verdict}", "red")
			elif verdict == UNVERIFIABLE and \
					not _navigates(_norm(command).split()[0]):
				logger.notify(f"{device.endpoint}: '{command.strip()}' not "
				              f"verifiable — it leaves nothing in the config",
				              "yellow")
		verified = verdicts.count(VERIFIED)
		checkable = verified + verdicts.count(NOT_CONFIGURED) + \
			verdicts.count(STILL_CONFIGURED)
		return VerifyResult(verified=verified, checkable=checkable,
		                    config=None if verified == checkable else config)

	def _verify(self, indexes: list[int],
	            logger: RolloutLogger) -> dict[int, "VerifyResult | None"]:
		"""Verifies the devices at `indexes` (those the push applied to)
		concurrently via ThreadPoolExecutor.
		:return: {device index: VerifyResult, or None if not fetched}"""
		result = {}
		with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
			futures = {executor.submit(self._verify_device, self.devices[idx],
			                           logger): idx for idx in indexes}
			for future in as_completed(futures):
				result[futures[future]] = future.result()
		return result

	@staticmethod
	def _log_summary(results: list[DeviceResultDict],
	                 logger: RolloutLogger) -> None:
		"""Final line, counted from the per-device statuses (it used to
		report every attempted device as configured)."""
		counts = Counter(r["status"] for r in results)
		parts = [f"{counts[s]} {s}" for s in
		         ("success", "partial", "failed", "cancelled") if counts[s]]
		ok = counts["success"]
		color = ("green" if ok == len(results)
		         else "red" if ok == 0 and not counts["partial"] else "yellow")
		logger.notify(f"Configuration rollout complete: "
		              f"{', '.join(parts)} (of {len(results)} devices)",
		              color, important=True)
		logger.notify(
			f"Please see Execution logs in {os.path.abspath(logger.logfile)}",
			important=True)

	def run(self, cancel_flag: threading.Event, logger: RolloutLogger) -> list[
		DeviceResultDict]:
		logger.notify("Starting configuration rollout", important=True)
		# Runs parse_files to subscribe data from the provided file paths
		# If parsing was successful and the output of the function was not empty lists, we continue the process
		if self.devices and self._commands:
			# Runs the config push procedure
			cancel_signal, push_results = self._push_config(cancel_flag, logger)

			# Verify only what the push applied to
			applied = [idx for idx, push in push_results.items() if push.applied]
			verify_results = {}
			if self._verify_flag and cancel_signal != "cancel_sent" and applied:
				logger.notify(
					"Configuration rollout finished. Initiating verification process",
					important=True)
				verify_results = self._verify(applied, logger)

			results = []
			total = len(self._commands)
			# the commands that configure something (not exit / end / next…)
			configuring = sum(1 for c in self._commands
			                  if not _navigates(_norm(c).split()[0] if c.strip() else ""))
			for idx, device in enumerate(self.devices):
				commands_verified, fetched_config = None, None
				if idx not in push_results:
					status, commands_sent = "cancelled", 0
				elif not push_results[idx].applied:
					status, commands_sent = "failed", 0
				else:
					commands_sent = total
					rejected = push_results[idx].rejected
					check = verify_results.get(idx)
					if check is not None:
						# not verifiable commands count as accounted for
						commands_verified = check.verified + total - check.checkable
						fetched_config = check.config
						logger.notify(f"{device.endpoint}: {check.verified}/"
						              f"{check.checkable} commands verified"
						              f"{f' ({total - check.checkable} not verifiable)' if total > check.checkable else ''}",
						              important=True)
						if check.verified == check.checkable and not rejected:
							status = "success"
						elif check.verified == 0 and check.checkable:
							status = "failed"
						else:
							status = "partial"
					else:
						# no verify (or the config couldn't be fetched): what
						# the device said while the commands were sent
						status = ("success" if not rejected else
						          "failed" if rejected >= configuring else "partial")
				results.append(DeviceResultDict(device_ip=device.ip,
				                                device_port=int(device.port),
				                                device_type=device.device_type,
				                                commands_sent=commands_sent,
				                                commands_verified=commands_verified,
				                                fetched_config=fetched_config,
				                                status=status))

			self._log_summary(results, logger)
			return results

		else:
			logger.notify("Device input invalid", "red")
			return []
