"""The rollout engine, shared by the CLI and the web app: push commands to
many devices over SSH (Netmiko) in parallel, finish each the way its
platform needs (save / commit / ...), optionally verify against the config
read back, and classify each device's outcome. Platform knowledge is in
src/platforms.py; this module does the I/O."""
import os
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, Optional, TypedDict

import netmiko
from netmiko import BaseConnection

from src import encryption
from src.logging_utils import RolloutLogger
from src.platforms import (COMMIT_TIMEOUT, FETCH_TIMEOUT, NOT_CONFIGURED,
                           PLATFORMS, STILL_CONFIGURED, UNVERIFIABLE, VERIFIED,
                           Platform, navigates, normalize, rejection,
                           verify_commands)
if TYPE_CHECKING:   # type hints only: the CLI (.exe) must not load the DB stack
	from src.db.tables import Inventory


class SubstitutionError(ValueError):
	"""A $$TOKEN$$ can't be resolved on a device."""


def endpoint(ip: str, port: int | str) -> str:
	"""How a target is written: ip:port, an IPv6 address in brackets
	([2001:db8::1]:22, as in a URL - its own colons would hide the port)."""
	return f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"


def mapping_resolvable(var_maps: dict[str, Any] | None, property_name: str,
                       index: int | None) -> bool:
	"""Whether a mapping can substitute on a device. Shared by the binding
	routes and the engine, so they can't disagree.

	:param var_maps: the device's attribute values (a text or a list each)
	:param property_name: the attribute the mapping reads
	:param index: a position in a list attribute (e.g. vrfs[2]); None: the
	 whole value
	:returns: the attribute is set and non-empty - and long enough, when
	 indexed"""
	value = (var_maps or {}).get(property_name)
	if not value:
		return False
	if index is None:
		return True
	return isinstance(value, list) and 0 <= index < len(value)


class PushResult(NamedTuple):
	"""How the push went on one device."""
	applied: bool    # the change took effect (connected, finished, committed)
	rejected: int    # commands the device refused


class VerifyResult(NamedTuple):
	"""How verify went on one device."""
	verified: int        # commands confirmed in the config
	checkable: int       # commands that can be checked (not navigation /
	                     # operational)
	config: str | None   # kept only when something didn't verify


def classify(push: PushResult | None, verify: VerifyResult | None,
             total: int, configuring: int) -> tuple[str, int, int | None]:
	"""A device's outcome, by these rules:
	- not applied (no connection, no commit) → failed, nothing counted
	- verified: every checkable command confirmed and none refused →
	  success; none confirmed (of some checkable) → failed; else partial.
	  Commands that can't be checked count as accounted for.
	- not verified: what the device said while the commands were sent —
	  none refused → success; every configuring one refused → failed;
	  else partial.

	:param push: None if the device never started (cancelled first)
	:param verify: None when verify was off or the config couldn't be fetched
	:param total: commands sent
	:param configuring: those that configure something (not navigation)
	:returns: (status, commands_sent, commands_verified - None unverified)"""
	if push is None:
		return "cancelled", 0, None
	if not push.applied:
		return "failed", 0, None
	if verify is not None:
		if verify.verified == verify.checkable and not push.rejected:
			status = "success"
		elif verify.verified == 0 and verify.checkable:
			status = "failed"
		else:
			status = "partial"
		return status, total, verify.verified + total - verify.checkable
	status = ("success" if not push.rejected else
	          "failed" if push.rejected >= configuring else "partial")
	return status, total, None


class DeviceResultDict(TypedDict):
	"""One device's outcome - a device_results row's fields."""
	device_ip: str
	device_port: int
	device_type: str
	commands_sent: int
	commands_verified: int | None
	fetched_config: str | None
	status: str
	action_needed: str | None   # what only a person can resolve, or None


@dataclass(slots=True, kw_only=True)
class RolloutOptions:
	"""How a rollout runs."""
	verify: bool = False
	verbose: bool = False
	webapp: bool = False      # log for the page (HTML) rather than a console
	max_workers: int = 10     # devices configured at the same time


@dataclass(kw_only=True)
class Device:
	"""A rollout target: where it is, how to log in (credentials decrypted),
	and its variable mappings with the attribute values they read."""
	ip: str
	label: str
	username: str
	password: str = field(repr=False)
	device_type: str
	secret: str = field(repr=False)
	port: int
	# $$TOKEN$$ -> (attribute name, index in a list attribute or None)
	var_map_subs: dict[str, tuple[str, int | None]] = field(
		default_factory=dict)
	# the attribute values (var_maps): text, or a list of texts
	extra: dict[str, Any] = field(default_factory=dict)

	@property
	def endpoint(self) -> str:
		"""ip:port — what identifies a reachable target (the IP alone doesn't:
		NAT / port forwarding put several devices behind one address)."""
		return endpoint(self.ip, self.port)

	def netmiko_connector(self) -> dict[str, str | int]:
		""":returns: Netmiko's ConnectHandler arguments for this device"""
		params: dict[str, str | int] = {
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
		credentials.

		:returns: the config; None if it can't be fetched (reported in the log)"""
		platform = PLATFORMS[self.device_type]
		try:
			with netmiko.ConnectHandler(**self.netmiko_connector()) as conn:
				if problem := _enter_cli_shell(conn, platform):
					logger.notify(f"could not fetch the config of {self.endpoint} "
					              f"to verify it: {problem}", "red")
					return None
				conn.enable()
				output = ""
				for command in platform.show_config:
					output = conn.send_command(command, read_timeout=FETCH_TIMEOUT)
				return output
		except Exception as e:
			logger.notify(f"could not fetch the config of {self.endpoint} to "
			              f"verify it: {e}", "red")
			return None

	@classmethod
	def from_inventory(cls, row: "Inventory", user_id: uuid.UUID) -> "Device":
		"""A rollout target from an inventory row, credentials decrypted.

		:param user_id: who rolls out - only their own mappings apply (the
		 join table is shared across users through global devices)
		:raises ValueError: the device has no security profile"""
		profile = row.security_profile
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


def _enter_cli_shell(conn: BaseConnection, platform: Platform) -> str | None:
	"""Leave a wrong login shell (Gaia expert / bash) for the CLI.

	:returns: the problem if the CLI shell couldn't be reached, else None"""
	if not platform.wrong_shell:
		return None

	def wrong(prompt: str) -> bool:
		prompt = prompt.strip().lower()
		return any(prompt.endswith(m) if m == "#" else m in prompt
		           for m in platform.wrong_shell)

	if not wrong(conn.find_prompt()):
		return None
	if platform.cli_shell:
		conn.send_command_timing(platform.cli_shell)
		conn.set_base_prompt()
		if not wrong(conn.find_prompt()):
			return None
	return (f"the account logs in to the wrong shell and "
	        f"'{platform.cli_shell or '?'}' didn't leave it — set its shell to "
	        f"clish")


def _fortios_save_if_manual(conn: BaseConnection) -> tuple[bool, str | None]:
	"""FortiOS saves changes automatically unless `cfg-save` is manual (lost
	at reboot) or revert (undone after a timeout) — then save explicitly.
	With VDOMs, the setting lives under `config global`.

	:returns: (saved explicitly, the device's complaint if saving failed)"""
	vdoms = getattr(conn, "_vdoms", False)
	if vdoms:
		conn.send_command_timing("config global")
	mode = conn.send_command_timing("get system global | grep cfg-save")
	saved, complaint = False, None
	if "manual" in mode or "revert" in mode:
		complaint = rejection(conn.send_command_timing("execute cfg save"),
		                      "execute cfg save")
		saved = complaint is None
	if vdoms:
		conn.send_command_timing("end")
	return saved, complaint


class RolloutEngine:
	"""One rollout: its devices, its commands and how it runs."""

	def __init__(self, param: RolloutOptions, devices: list[Device],
	             commands: list[str]) -> None:
		""":param param: verify, the parallelism (and how it logs)
		:param commands: pushed in order, $$TOKEN$$s resolved per device"""
		self._devices = devices
		# endpoint → what only a person can resolve there (Results page,
		# summary)
		self._needs_action: dict[str, list[str]] = {}
		self._verify_flag = param.verify
		self._max_workers = param.max_workers
		self._commands = commands

	@property
	def device_count(self) -> int:
		""":returns: how many devices it targets"""
		return len(self._devices)

	def cancelled_results(self) -> list["DeviceResultDict"]:
		"""Every device recorded as cancelled — for a job that never
		started."""
		return [DeviceResultDict(device_ip=d.ip, device_port=int(d.port),
		                         device_type=d.device_type, commands_sent=0,
		                         commands_verified=None, fetched_config=None,
		                         status="cancelled", action_needed=None)
		        for d in self._devices]

	def _substitute_commands(self, device: Device) -> list[str]:
		"""The commands with the device's $$TOKEN$$s replaced by its values.

		:raises SubstitutionError: a mapped attribute is missing on the
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
		:param cancel_event: set: a device not connected yet is skipped
		:returns: (ip, PushResult) — applied False when nothing took effect
		 (no connection, a failed commit) — or (ip, None) if cancelled before
		 connecting"""
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
		logger.notify(f"connecting to {device.endpoint}", "yellow")
		commands_sent = False
		try:
			net_connect = netmiko.ConnectHandler(**(device.netmiko_connector()))
			try:
				logger.notify(f"{device.ip} connected successfully", "green")
				# Goes into privileged config mode, depending on the platform
				if problem := _enter_cli_shell(net_connect, platform):
					self._action_needed(device, f"{problem} (nothing was sent)",
					                    logger)
					return device.ip, PushResult(applied=False, rejected=0)
				net_connect.enable()
				if platform.config_command:
					try:
						net_connect.config_mode(
							config_command=platform.config_command)
					except (ValueError, netmiko.exceptions.ReadTimeout) as e:
						self._action_needed(
							device, f"'{platform.config_command}' was refused — "
							f"usually another session has uncommitted changes in "
							f"the shared configuration: commit or discard them "
							f"(they're someone else's work), then rerun. Nothing "
							f"was sent ({e})", logger)
						return device.ip, PushResult(applied=False, rejected=0)
				else:
					net_connect.config_mode()

				rejected = 0
				for command in commands:
					commands_sent = True
					# Config mode was entered above: each command goes in exactly
					# as typed. Netmiko's default re-checks config mode per call,
					# and drivers that only recognise their top-level config
					# prompt (Aruba CX: "(config)#") then fail inside a section
					output = net_connect.send_config_set(
						[command.strip()], enter_config_mode=False,
						exit_config_mode=False)
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
				# Prompt changed mid-session (e.g. a new hostname): the commands
				# were applied, but the finish couldn't run in this session —
				# a fresh one learns the new prompt
				self._finish_in_new_session(device, platform, logger)
				return device.ip, PushResult(applied=True, rejected=0)
			logger.notify(f"{device.ip} failed: {e}", "red")
			return device.ip, PushResult(applied=False, rejected=0)
		except Exception as e:
			logger.notify(f"{device.ip} failed: {e}", "red")
			return device.ip, PushResult(applied=False, rejected=0)

	def _action_needed(self, device: Device, what: str,
	                   logger: RolloutLogger) -> None:
		"""Something only a person can do on the device: one unmistakable
		line (live log, log file, CLI console), counted in the summary."""
		self._needs_action.setdefault(device.endpoint, []).append(what)
		logger.notify(f"ACTION NEEDED — {device.endpoint}: {what}", "red",
		              important=True)

	def _finish_in_new_session(self, device: Device, platform: Platform,
	                           logger: RolloutLogger) -> None:
		"""Save from a fresh session after the old one lost its prompt."""
		if not platform.finish:
			logger.notify(f"{device.endpoint}: prompt changed after the push "
			              f"(e.g. a new hostname) — applied", "yellow")
			return
		try:
			with netmiko.ConnectHandler(**device.netmiko_connector()) as conn:
				conn.enable()
				if platform.finish == "save":
					reply, command = conn.save_config(), "save"
				else:
					reply = conn.send_command(platform.finish)
					command = platform.finish
			if isinstance(reply, str) and (complaint := rejection(reply, command)):
				raise RuntimeError(complaint)
			logger.notify(f"{device.endpoint}: prompt changed after the push (e.g. "
			              f"a new hostname) — applied, and saved from a new "
			              f"session", "yellow")
		except Exception as e:
			self._action_needed(device, f"the change is live but NOT saved — "
			                    f"save it on the device (the prompt changed "
			                    f"after the push and saving from a new session "
			                    f"failed: {e})", logger)

	def _finish(self, conn: BaseConnection, platform: Platform, device: Device,
	            logger: RolloutLogger) -> bool:
		"""Finish a pushed device the way its platform needs: close FortiOS
		blocks (saving when cfg-save isn't automatic), commit, save, or a
		finishing command.

		:param conn: the push's open session
		:returns: False if the change didn't take effect (a failed commit)"""
		if platform.close_blocks:
			# FortiOS applies a block at "end"; one left open is discarded
			# (and Netmiko's own cleanup would run inside it)
			for _ in range(10):
				if "(" not in conn.find_prompt():
					break
				if complaint := rejection(conn.send_command_timing("end"), "end"):
					logger.notify(f"{device.endpoint}: closing a config block "
					              f"failed — {complaint}", "red")
			saved, complaint = _fortios_save_if_manual(conn)
			if complaint:
				self._action_needed(device, f"cfg-save is not automatic and "
				                    f"saving failed — run 'execute cfg save' on "
				                    f"the device, or the change is lost at reboot "
				                    f"/ reverted ({complaint})", logger)
			elif saved:
				logger.notify(f"{device.endpoint}: cfg-save is not automatic — "
				              f"configuration saved", "yellow")
		if platform.finish == "commit":
			# Before leaving config mode: leaving discards uncommitted changes
			try:
				conn.commit(read_timeout=COMMIT_TIMEOUT)
			except netmiko.exceptions.ReadTimeout:
				# It may still complete on the device: don't claim either way
				self._action_needed(device, f"the commit didn't finish within "
				                    f"{COMMIT_TIMEOUT}s — check on the device "
				                    f"whether it went through (reported as failed)",
				                    logger)
				return False
			except ValueError as e:
				logger.notify(f"{device.endpoint}: commit failed — nothing was "
				              f"applied. {e}", "red")
				discarded = False
				for command in platform.discard:
					reply = conn.send_config_set([command], exit_config_mode=False)
					if rejection(reply, command) is None:
						discarded = True
						break
				if not discarded and platform.finish == "commit" and \
						device.device_type != "cisco_xr":
					self._action_needed(device, "the failed changes may still be "
					                    "in the candidate configuration — discard "
					                    "them on the device", logger)
				conn.exit_config_mode()
				return False
		if platform.leave_first:
			conn.send_command_timing(platform.leave_first)
		conn.exit_config_mode()
		reply, command = "", ""
		if platform.finish == "save":
			reply, command = conn.save_config(), "save"
		elif platform.finish and platform.finish != "commit":
			reply, command = conn.send_command(platform.finish), platform.finish
		# The change is live either way; saving failing (e.g. Gaia's config
		# lock held by another session) means it's lost at the next reboot
		if command and isinstance(reply, str) and \
				(complaint := rejection(reply, command)):
			self._action_needed(device, f"the change is live but NOT saved — "
			                    f"save it on the device ({complaint})", logger)
		return True

	def _push_config(self, cancel_event: threading.Event,
	                 logger: RolloutLogger) -> tuple[
		str | None, dict[int, "PushResult"]]:
		"""Push every device, max_workers at a time.
		A cancel stops devices that haven't connected yet; devices already
		mid-push finish their commands (a half-applied config is worse than
		either state). Every result is collected — returning at the first
		cancelled device used to drop the results of devices still in flight,
		recording them as cancelled although their config was applied.

		:param cancel_event: set by a cancel (the page) or Ctrl+C (the CLI)
		:returns: (cancel_signal, push_results) where cancel_signal is
		 "cancel_sent" or None and push_results maps each device's index in
		 self._devices to its result; devices that never connected are absent"""
		# Keyed by position, not IP: several devices can share an IP (NAT /
		# port forwarding, overlapping address space) and must not overwrite
		# each other's result
		push_results = {}
		cancelled = False
		with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
			futures = {
				executor.submit(self._push_device, device, cancel_event,
				                logger): idx
				for idx, device in enumerate(self._devices)
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
		:returns: the counts, the config kept only when something didn't
		 verify (for Verify Diff) — or None if the config couldn't be fetched"""
		try:
			expected = self._substitute_commands(device)
		except SubstitutionError:
			return None   # already reported (and the device failed) at the push
		config = device.fetch_config(logger)
		if config is None:
			# Couldn't verify ≠ not configured: the status comes from the push,
			# but nobody checked the result — say so where it can't be missed
			self._action_needed(device, "the change was applied but NOT "
			                    "verified — its config couldn't be read (the "
			                    "reason is in the log): check it on the device",
			                    logger)
			return None
		verdicts = verify_commands(device.device_type, config, expected)
		for command, verdict in zip(expected, verdicts):
			if verdict in (NOT_CONFIGURED, STILL_CONFIGURED):
				logger.notify(f"{device.endpoint}: '{command.strip()}' "
				              f"{verdict}", "red")
			elif verdict == UNVERIFIABLE and \
					not navigates(normalize(command).split()[0]):
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
		:returns: {device index: VerifyResult, or None if not fetched}"""
		result: dict[int, VerifyResult | None] = {}
		with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
			futures = {executor.submit(self._verify_device, self._devices[idx],
			                           logger): idx for idx in indexes}
			for future in as_completed(futures):
				result[futures[future]] = future.result()
		return result

	def _log_summary(self, results: list[DeviceResultDict],
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
		if self._needs_action:
			count = len(self._needs_action)
			logger.notify(f"ACTION NEEDED on {count} device"
			              f"{'s' if count != 1 else ''} "
			              f"({', '.join(sorted(self._needs_action))}) — see the "
			              f"lines marked ACTION NEEDED", "red", important=True)
		logger.notify(
			f"Please see Execution logs in {os.path.abspath(logger.logfile)}",
			important=True)

	def run(self, cancel_flag: threading.Event, logger: RolloutLogger) -> list[
		DeviceResultDict]:
		"""The whole rollout: push, verify what was applied (when on), classify
		each device, log the summary.

		:param cancel_flag: set to cancel - devices not reached yet are skipped
		:returns: one result per device, in the devices' order; [] when there
		 are no devices or no commands"""
		logger.notify("Starting configuration rollout", important=True)
		if self._devices and self._commands:
			cancel_signal, push_results = self._push_config(cancel_flag, logger)

			# Verify only what the push applied to
			applied = [idx for idx, push in push_results.items() if push.applied]
			verify_results: dict[int, VerifyResult | None] = {}
			if self._verify_flag and cancel_signal != "cancel_sent" and applied:
				logger.notify(
					"Configuration rollout finished. Initiating verification process",
					important=True)
				verify_results = self._verify(applied, logger)

			results: list[DeviceResultDict] = []
			total = len(self._commands)
			# the commands that configure something (not exit / end / next…)
			configuring = sum(1 for c in self._commands
			                  if not navigates(normalize(c).split()[0] if c.strip() else ""))
			for idx, device in enumerate(self._devices):
				push = push_results.get(idx)
				check = verify_results.get(idx) if push and push.applied else None
				status, commands_sent, commands_verified = classify(
					push, check, total, configuring)
				fetched_config = check.config if check else None
				if check is not None:
					logger.notify(f"{device.endpoint}: {check.verified}/"
					              f"{check.checkable} commands verified"
					              f"{f' ({total - check.checkable} not verifiable)' if total > check.checkable else ''}",
					              important=True)
				results.append(DeviceResultDict(device_ip=device.ip,
				                                device_port=int(device.port),
				                                device_type=device.device_type,
				                                commands_sent=commands_sent,
				                                commands_verified=commands_verified,
				                                fetched_config=fetched_config,
				                                status=status,
				                                action_needed="\n".join(
					                                self._needs_action.get(
						                                device.endpoint, []))
				                                or None))

			self._log_summary(results, logger)
			return results

		else:
			logger.notify("Device input invalid", "red")
			return []
