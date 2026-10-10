"""The rollout engine, shared by the CLI and the web app: push commands to
many devices over SSH (Netmiko) in parallel, finish each the way its
platform needs (save / commit / ...), optionally verify against the config
read back, and classify each device's outcome. Platform knowledge is in
src/rollout/platforms.py, one device's SSH conversation in
src/rollout/session.py; this module runs the rollout across the devices."""
import os
import re
import threading
import uuid
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

from src import encryption
from src.rollout.log import Tone
from src.rollout.platforms import NOT_CONFIGURED, STILL_CONFIGURED, UNVERIFIABLE, VERIFIED, first_word, navigates, verify_commands
from src.rollout.session import NetmikoSession, Notifier, PushResult, RunReport
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


def missing_value(var_maps: dict[str, Any] | None, property_name: str,
                  index: int | None) -> str | None:
	"""Why a mapping can't substitute on a device, in the words unresolved()
	and the New Rollout page use (see mapping_resolvable).

	:returns: e.g. "no value for 'vrfs[2]'"; None when it can"""
	if mapping_resolvable(var_maps, property_name, index):
		return None
	where = property_name if index is None else f"{property_name}[{index}]"
	return f"no value for '{where}'"


# a $$TOKEN$$ in a command (a mapping's token shape: inputs.token_problem); a
# lone $$ isn't one
TOKEN_IN_COMMAND = re.compile(r"\$\$[A-Za-z0-9_]{1,64}\$\$")


class DeviceStatus(StrEnum):
	"""A device's outcome in a rollout (device_results.status; a job's status
	is one of them too: src/results.job_status), in the order the summary and
	the Results filter list them."""
	SUCCESS = "success"
	PARTIAL = "partial"
	FAILED = "failed"
	CANCELLED = "cancelled"


class VerifyResult(NamedTuple):
	"""How verify went on one device."""
	verified: int        # commands confirmed in the config
	checkable: int       # commands that can be checked (not navigation /
	                     # operational)
	config: str | None   # kept only when something didn't verify


def classify(push: PushResult | None, verify: VerifyResult | None,
             total: int, configuring: int) -> tuple[DeviceStatus, int, int | None]:
	"""A device's outcome, by these rules:
	- not applied (no connection, no commit) → failed, nothing counted
	- verified: every checkable command confirmed and none refused →
	  success; none confirmed (of some checkable) → failed; else partial.
	  Commands that can't be checked count as accounted for.
	- not verified: what the device said while the commands were sent —
	  none refused → success; every configuring one refused → failed;
	  else partial.
	- interrupted (the device stopped answering before the last command,
	  some commands confirmed) → partial, the commands sent counted.

	:param push: None if the device never started (cancelled first)
	:param verify: None when verify was off or the config couldn't be fetched
	:param total: commands sent
	:param configuring: those that configure something (not navigation)
	:returns: (status, commands_sent, commands_verified - None unverified)"""
	if push is None:
		return DeviceStatus.CANCELLED, 0, None
	if not push.applied:
		return DeviceStatus.FAILED, 0, None
	if push.interrupted:
		# some commands took effect (live, not saved), the rest weren't sent
		return DeviceStatus.PARTIAL, push.sent or 0, verify.verified if verify is not None else None
	if verify is not None:
		if verify.verified == verify.checkable and not push.rejected:
			status = DeviceStatus.SUCCESS
		elif verify.verified == 0 and verify.checkable:
			status = DeviceStatus.FAILED
		else:
			status = DeviceStatus.PARTIAL
		return status, total, verify.verified + total - verify.checkable
	status = (DeviceStatus.SUCCESS if not push.rejected else
	          DeviceStatus.FAILED if push.rejected >= configuring
	          else DeviceStatus.PARTIAL)
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
	# the attribute values the rolling-out user sees: text, or a list of texts
	extra: dict[str, Any] = field(default_factory=dict)

	@property
	def endpoint(self) -> str:
		"""ip:port — what identifies a reachable target (the IP alone doesn't:
		NAT / port forwarding put several devices behind one address)."""
		return endpoint(self.ip, self.port)

	def unresolved(self, commands: list[str]) -> list[str]:
		"""Why the commands can't be filled in for this device - checked by the
		launch and rollback routes before a rollout is queued, and again at the
		push (a value can go while a job waits).

		:param commands: the commands it would get
		:returns: one line per token the commands use that has no mapping on
		 the device, or whose value is missing (an index past the end); in
		 order, each once; [] when every token resolves"""
		problems: list[str] = []
		seen: set[str] = set()
		for command in commands:
			for token in TOKEN_IN_COMMAND.findall(command):
				if token in seen:
					continue
				seen.add(token)
				if token not in self.var_map_subs:
					problems.append(f"{token}: no mapping on this device")
					continue
				property_name, index = self.var_map_subs[token]
				if missing := missing_value(self.extra, property_name, index):
					problems.append(f"{token}: {missing}")
		return problems

	def commands_for(self, commands: list[str]) -> list[str]:
		"""The commands with the device's $$TOKEN$$s replaced by its values.

		:raises SubstitutionError: a token the commands use can't be filled in
		 (unresolved: no mapping on the device, or its value gone - e.g.
		 removed from a global device while the job was queued)"""
		if problems := self.unresolved(commands):
			raise SubstitutionError("; ".join(problems))
		commands_copy = commands.copy()
		for token, (property_name, index) in self.var_map_subs.items():
			if not any(token in command for command in commands_copy):
				continue      # bound, but not used by these commands
			property_value = self.extra[property_name]
			if index is not None:
				property_value = property_value[index]

			property_value = str(property_value).strip()
			commands_copy = [command.replace(token, property_value)
			                 for command in commands_copy]
		return commands_copy

	@classmethod
	def from_inventory(cls, row: "Inventory", user_id: uuid.UUID,
	                   attributes: dict[str, Any] | None = None) -> "Device":
		"""A rollout target from an inventory row, credentials decrypted.

		:param user_id: who rolls out - only their own mappings apply (the
		 join table is shared across users through global devices)
		:param attributes: the device's attribute values as that user sees
		 them (the web app: InventoryView.attributes - the device's system
		 values and the user's own custom ones); None: the row's var_maps
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
		           extra=(row.var_maps or {}) if attributes is None else attributes)


class RolloutEngine:
	"""One rollout: its devices, its commands and how it runs."""

	def __init__(self, param: RolloutOptions, devices: list[Device],
	             commands: list[str]) -> None:
		""":param param: verify, the parallelism (and how it logs)
		:param commands: pushed in order, $$TOKEN$$s resolved per device"""
		self._devices = devices
		self._verify_flag = param.verify
		self._max_workers = param.max_workers
		self._commands = commands
		self._report: RunReport | None = None   # the run's, from its logger

	@property
	def device_count(self) -> int:
		""":returns: how many devices it targets"""
		return len(self._devices)

	def cancelled_results(self) -> list[DeviceResultDict]:
		"""Every device recorded as cancelled — for a job that never
		started."""
		return [DeviceResultDict(device_ip=d.ip, device_port=int(d.port),
		                         device_type=d.device_type, commands_sent=0,
		                         commands_verified=None, fetched_config=None,
		                         status=DeviceStatus.CANCELLED, action_needed=None)
		        for d in self._devices]

	def _reporting(self, notifier: Notifier) -> RunReport:
		""":returns: the rollout's report, logging through `notifier` (made
		 at its first use)"""
		if self._report is None:
			self._report = RunReport(notifier)
		return self._report

	def _push_device(self, device: Device, cancel_event: threading.Event,
	                 report: RunReport) -> PushResult | None:
		"""One device's push (NetmikoSession.push), its $$TOKEN$$s resolved
		first. Called concurrently by _push_config via ThreadPoolExecutor.

		:param cancel_event: set: a device not connected yet is skipped
		:returns: its PushResult; None if cancelled before connecting"""
		if cancel_event and cancel_event.is_set():
			return None
		# Resolve $$TOKEN$$s before opening SSH: a device whose mappings can't
		# resolve fails on its own with a clear reason, and is never touched
		try:
			commands = device.commands_for(self._commands)
		except SubstitutionError as e:
			report.notify(f"{device.endpoint} skipped — {e}", Tone.ERROR)
			return PushResult(applied=False, rejected=0)
		return NetmikoSession(device, report).push(commands)

	def _push_config(self, cancel_event: threading.Event,
	                 notifier: Notifier) -> tuple[bool, dict[int, PushResult]]:
		"""Push every device, max_workers at a time.
		A cancel stops devices that haven't connected yet; devices already
		mid-push finish their commands (a half-applied config is worse than
		either state). Every result is collected — returning at the first
		cancelled device used to drop the results of devices still in flight,
		recording them as cancelled although their config was applied.

		:param cancel_event: set by a cancel (the page) or Ctrl+C (the CLI)
		:returns: (cancelled, push_results): push_results maps each device's
		 index in self._devices to its result; devices that never connected
		 are absent"""
		report = self._reporting(notifier)
		# Keyed by position, not IP: several devices can share an IP (NAT /
		# port forwarding, overlapping address space) and must not overwrite
		# each other's result
		push_results: dict[int, PushResult] = {}
		cancelled = False

		def collect(done: Iterable[Future[PushResult | None]]) -> None:
			nonlocal cancelled
			for future in done:
				result = future.result()
				if result is None:
					if not cancelled:
						report.notify("Rollout Canceled By User", Tone.ERROR)
					cancelled = True
					continue
				push_results[futures[future]] = result

		executor = ThreadPoolExecutor(max_workers=self._max_workers)
		try:
			futures = {
				executor.submit(self._push_device, device, cancel_event,
				                report): idx
				for idx, device in enumerate(self._devices)
			}
			try:
				collect(as_completed(futures))
			except KeyboardInterrupt:
				# Ctrl+C (the CLI): the devices not reached yet are skipped,
				# those being configured finish and are recorded; a second
				# Ctrl+C leaves at once (the CLI exits without waiting)
				cancel_event.set()
				report.notify("Interrupted - the devices being configured now "
				              "finish, the rest are skipped (Ctrl+C again to "
				              "quit at once)", Tone.ERROR, important=True)
				cancelled = True
				collect(as_completed([f for f in futures
				                      if futures[f] not in push_results]))
		finally:
			# not waiting here: after a second Ctrl+C the CLI leaves at once
			executor.shutdown(wait=False, cancel_futures=True)
		return cancelled, push_results

	def _verify_device(self, device: Device,
	                   report: RunReport) -> VerifyResult | None:
		"""Fetches the device's config and checks every command in it
		(verify_commands). Called concurrently by _verify.
		:returns: the counts, the config kept only when something didn't
		 verify (for Verify Diff) — or None if the config couldn't be fetched"""
		try:
			expected = device.commands_for(self._commands)
		except SubstitutionError:
			return None   # already reported (and the device failed) at the push
		config = NetmikoSession(device, report).fetch_config()
		if config is None:
			# Couldn't verify ≠ not configured: the status comes from the push,
			# but nobody checked the result — say so where it can't be missed
			report.action_needed(device, "the change was applied but NOT "
			                     "verified — its config couldn't be read (the "
			                     "reason is in the log): check it on the device")
			return None
		verdicts = verify_commands(device.device_type, config, expected)
		for command, verdict in zip(expected, verdicts):
			if verdict in (NOT_CONFIGURED, STILL_CONFIGURED):
				report.notify(f"{device.endpoint}: '{command.strip()}' "
				              f"{verdict}", Tone.ERROR)
			elif verdict == UNVERIFIABLE and \
					not navigates(first_word(command)):
				report.notify(f"{device.endpoint}: '{command.strip()}' not "
				              f"verifiable — it leaves nothing in the config",
				              Tone.WARNING)
		verified = verdicts.count(VERIFIED)
		checkable = verified + verdicts.count(NOT_CONFIGURED) + \
			verdicts.count(STILL_CONFIGURED)
		return VerifyResult(verified=verified, checkable=checkable,
		                    config=None if verified == checkable else config)

	def _verify(self, indexes: list[int],
	            notifier: Notifier) -> dict[int, VerifyResult | None]:
		"""Verifies the devices at `indexes` (those the push applied to)
		concurrently via ThreadPoolExecutor.
		:returns: {device index: VerifyResult, or None if not fetched}"""
		report = self._reporting(notifier)
		result: dict[int, VerifyResult | None] = {}
		with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
			futures = {executor.submit(self._verify_device, self._devices[idx],
			                           report): idx for idx in indexes}
			for future in as_completed(futures):
				result[futures[future]] = future.result()
		return result

	def _results(self, push_results: dict[int, PushResult],
	             verify_results: dict[int, VerifyResult | None],
	             report: RunReport) -> list[DeviceResultDict]:
		""":returns: each device's outcome (classify), in the devices' order"""
		results: list[DeviceResultDict] = []
		total = len(self._commands)
		# the commands that configure something (not exit / end / next…)
		configuring = sum(1 for c in self._commands
		                  if not navigates(first_word(c)))
		for idx, device in enumerate(self._devices):
			push = push_results.get(idx)
			check = verify_results.get(idx) if push and push.applied else None
			status, commands_sent, commands_verified = classify(
				push, check, total, configuring)
			if check is not None:
				report.notify(f"{device.endpoint}: {check.verified}/"
				              f"{check.checkable} commands verified"
				              f"{f' ({total - check.checkable} not verifiable)' if total > check.checkable else ''}",
				              important=True)
			results.append(DeviceResultDict(device_ip=device.ip,
			                                device_port=int(device.port),
			                                device_type=device.device_type,
			                                commands_sent=commands_sent,
			                                commands_verified=commands_verified,
			                                fetched_config=check.config if check else None,
			                                status=status,
			                                action_needed=report.actions(device.endpoint)))
		return results

	def _log_summary(self, results: list[DeviceResultDict],
	                 report: RunReport) -> None:
		"""Final line, counted from the per-device statuses (it used to
		report every attempted device as configured)."""
		counts = Counter(r["status"] for r in results)
		parts = [f"{counts[s]} {s}" for s in DeviceStatus if counts[s]]
		ok = counts[DeviceStatus.SUCCESS]
		color = (Tone.SUCCESS if ok == len(results)
		         else Tone.ERROR if ok == 0 and not counts[DeviceStatus.PARTIAL] else Tone.WARNING)
		report.notify(f"Configuration rollout complete: "
		              f"{', '.join(parts)} (of {len(results)} devices)",
		              color, important=True)
		if report.needs_action:
			count = len(report.needs_action)
			report.notify(f"ACTION NEEDED on {count} device"
			              f"{'s' if count != 1 else ''} "
			              f"({', '.join(sorted(report.needs_action))}) — see the "
			              f"lines marked ACTION NEEDED", Tone.ERROR, important=True)
		report.notify(
			f"Please see Execution logs in {os.path.abspath(report.notifier.logfile)}",
			important=True)

	def run(self, cancel_flag: threading.Event, logger: Notifier) -> list[
		DeviceResultDict]:
		"""The whole rollout: push, verify what was applied (when on), classify
		each device, log the summary.

		:param cancel_flag: set to cancel - devices not reached yet are skipped
		:param logger: where it logs (a RolloutLogger)
		:returns: one result per device, in the devices' order; [] when there
		 are no devices or no commands"""
		report = self._reporting(logger)
		report.notify("Starting configuration rollout", important=True)
		if not (self._devices and self._commands):
			report.notify("Device input invalid", Tone.ERROR)
			return []
		cancelled, push_results = self._push_config(cancel_flag, logger)

		# Verify only what the push applied to
		applied = [idx for idx, push in push_results.items() if push.applied]
		verify_results: dict[int, VerifyResult | None] = {}
		if self._verify_flag and not cancelled and applied:
			report.notify(
				"Configuration rollout finished. Initiating verification process",
				important=True)
			verify_results = self._verify(applied, logger)

		results = self._results(push_results, verify_results, report)
		self._log_summary(results, report)
		return results
