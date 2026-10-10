"""One device's SSH conversation in a rollout (Netmiko): the push - into the
CLI shell and config mode, the commands line by line, the platform's finish
- and the config fetch for verify; and the rollout's report (RunReport),
which the sessions write and the engine reads."""
from __future__ import annotations   # type hints are never evaluated

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

import netmiko

from src.rollout.log import Tone, redact
from src.rollout.platforms import FETCH_TIMEOUT, PLATFORMS, ConfigSession, Platform, Target, rejection
if TYPE_CHECKING:   # annotations only: engine.py imports this module
	from src.rollout.engine import Device


class Notifier(Protocol):
	"""What a rollout logs through (src/rollout/log.RolloutLogger)."""
	logfile: str

	def notify(self, message: str, color: str = Tone.INFO,
	           important: bool = False) -> None: ...


class RunReport:
	"""What a rollout tells: its log lines, and what only a person can
	resolve on each device (the Results page, the summary). Written by the
	sessions' worker threads: one dict write per call, no lock needed (as
	before - the GIL keeps setdefault / append whole)."""

	def __init__(self, notifier: Notifier) -> None:
		self.notifier = notifier
		# endpoint → what only a person can resolve there
		self.needs_action: dict[str, list[str]] = {}

	def notify(self, text: str, tone: Tone = Tone.INFO,
	           important: bool = False) -> None:
		"""A log line (see RolloutLogger.notify)."""
		self.notifier.notify(text, tone, important=important)

	def action_needed(self, device: Target, what: str) -> None:
		"""Something only a person can do on the device: one unmistakable
		line (live log, log file, CLI console), counted in the summary - its
		text kept for the database (Results, the summary) redacted, as the
		log line is."""
		what = redact(what)
		self.needs_action.setdefault(device.endpoint, []).append(what)
		self.notify(f"ACTION NEEDED — {device.endpoint}: {what}", Tone.ERROR,
		            important=True)

	def actions(self, endpoint: str) -> str | None:
		""":returns: what a person must do on that device, a line each; None
		 when nothing"""
		return "\n".join(self.needs_action.get(endpoint, [])) or None


class PushResult(NamedTuple):
	"""How the push went on one device."""
	applied: bool    # the change took effect (connected, finished, committed)
	rejected: int    # commands the device refused
	# the device stopped answering before the last command: how many were
	# sent (the one without an answer included); the rest weren't, nothing
	# was saved
	sent: int | None = None
	interrupted: bool = False


def _connect_args(device: Device) -> dict[str, str | int]:
	""":returns: Netmiko's ConnectHandler arguments for this device"""
	return {
		"ip": device.ip,
		"username": device.username,
		"password": device.password,
		"device_type": device.device_type,
		"port": device.port,
		"secret": device.secret
	}


def _enter_cli_shell(conn: ConfigSession, platform: Platform) -> str | None:
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


def _fortios_save_if_manual(conn: ConfigSession) -> tuple[bool, str | None]:
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


class NetmikoSession:
	"""One device's SSH conversation: its device, its platform, and the
	rollout's report it writes to."""

	def __init__(self, device: Device, report: RunReport) -> None:
		self._device = device
		self._platform = PLATFORMS[device.device_type]
		self._report = report

	@staticmethod
	@contextmanager
	def connect(device: Device) -> Iterator[ConfigSession]:
		"""An SSH session to the device (its port and credentials), closed
		on leaving - a connection test's.

		:raises: Netmiko's exceptions when it can't connect"""
		conn = netmiko.ConnectHandler(**_connect_args(device))
		try:
			yield conn
		finally:
			conn.disconnect()

	def push(self, commands: list[str]) -> PushResult:
		"""Push the commands (resolved for this device) over SSH, then finish
		them the way the platform needs (save, commit, a command or nothing).
		A command the device refuses is reported with its reply and the rest
		are still sent.

		:returns: applied False when nothing took effect (no connection, a
		 failed commit)"""
		device, platform, report = self._device, self._platform, self._report
		report.notify(f"connecting to {device.endpoint}", Tone.WARNING)
		commands_sent = False
		sent = rejected = 0      # commands sent so far, and refused
		try:
			net_connect = netmiko.ConnectHandler(**_connect_args(device))
			try:
				report.notify(f"{device.ip} connected successfully", Tone.SUCCESS)
				# Goes into privileged config mode, depending on the platform
				if problem := _enter_cli_shell(net_connect, platform):
					report.action_needed(device, f"{problem} (nothing was sent)")
					return PushResult(applied=False, rejected=0)
				net_connect.enable()
				if platform.config_command:
					try:
						net_connect.config_mode(
							config_command=platform.config_command)
					except (ValueError, netmiko.exceptions.ReadTimeout) as e:
						report.action_needed(
							device, f"'{platform.config_command}' was refused — "
							f"usually another session has uncommitted changes in "
							f"the shared configuration: commit or discard them "
							f"(they're someone else's work), then rerun. Nothing "
							f"was sent ({e})")
						return PushResult(applied=False, rejected=0)
				else:
					net_connect.config_mode()

				for command in commands:
					commands_sent = True
					sent += 1
					# Config mode was entered above: each command goes in exactly
					# as typed. Netmiko's default re-checks config mode per call,
					# and drivers that only recognise their top-level config
					# prompt (Aruba CX: "(config)#") then fail inside a section
					output = net_connect.send_config_set(
						[command.strip()], enter_config_mode=False,
						exit_config_mode=False)
					if complaint := rejection(output, command.strip()):
						rejected += 1
						report.notify(f"{device.endpoint}: '{command.strip()}' "
						              f"rejected — {complaint}", Tone.ERROR)

				applied = self._finish(net_connect)
				return PushResult(applied=applied, rejected=rejected)
			finally:
				# Always closed, also when the push fails half-way
				try:
					net_connect.disconnect()
				except Exception:
					pass

		# In case of exception or issue in connecting and executing the commands,
		# an error message will be printed, and we move to the next device
		except netmiko.NetMikoAuthenticationException:
			report.notify(f"{device.ip} authentication failed", Tone.ERROR)
			return PushResult(applied=False, rejected=0)
		except netmiko.NetmikoTimeoutException:
			report.notify(f"{device.ip} timed out", Tone.ERROR)
			return PushResult(applied=False, rejected=0)
		except netmiko.exceptions.ReadTimeout as e:
			if commands_sent and platform.finish.interruptible and \
					sent == len(commands):
				# The last command changed the prompt (e.g. a new hostname):
				# the commands were applied, but the finish couldn't run in this
				# session — a fresh one learns the new prompt
				self._finish_in_new_session()
				return PushResult(applied=True, rejected=rejected)
			if commands_sent and platform.finish.interruptible:
				# No answer before the last command (a question without a
				# prompt, a slow command): the rest isn't sent, nothing saved
				answered = sent - 1
				report.action_needed(device, (
					f"stopped answering after '{commands[sent - 1].strip()}' "
					f"(command {sent} of {len(commands)}): "
					+ (f"the {answered} before it {'is' if answered == 1 else 'are'} "
					   f"live but NOT saved, " if answered else "")
					+ f"the {len(commands) - sent} after it weren't sent — check "
					  f"the device, then save the change or remove it"))
				took_effect = answered - rejected > 0
				return PushResult(applied=took_effect, rejected=rejected,
				                  sent=sent, interrupted=True)
			report.notify(f"{device.ip} failed: {e}", Tone.ERROR)
			return PushResult(applied=False, rejected=0)
		except Exception as e:
			report.notify(f"{device.ip} failed: {e}", Tone.ERROR)
			return PushResult(applied=False, rejected=0)

	def _finish(self, conn: ConfigSession) -> bool:
		"""Finish a pushed device the way its platform needs: close FortiOS
		blocks (saving when cfg-save isn't automatic), then the platform's
		Finish around leaving config mode.

		:param conn: the push's open session
		:returns: False if the change didn't take effect (a failed commit)"""
		device, platform, report = self._device, self._platform, self._report
		if platform.close_blocks:
			# FortiOS applies a block at "end"; one left open is discarded
			# (and Netmiko's own cleanup would run inside it)
			for _ in range(10):
				if "(" not in conn.find_prompt():
					break
				if complaint := rejection(conn.send_command_timing("end"), "end"):
					report.notify(f"{device.endpoint}: closing a config block "
					              f"failed — {complaint}", Tone.ERROR)
			saved, complaint = _fortios_save_if_manual(conn)
			if complaint:
				report.action_needed(device, f"cfg-save is not automatic and "
				                     f"saving failed — run 'execute cfg save' on "
				                     f"the device, or the change is lost at reboot "
				                     f"/ reverted ({complaint})")
			elif saved:
				report.notify(f"{device.endpoint}: cfg-save is not automatic — "
				              f"configuration saved", Tone.WARNING)
		# Before leaving config mode: leaving discards uncommitted changes
		if not platform.finish.before_leave(conn, report, device):
			return False
		if platform.leave_first:
			conn.send_command_timing(platform.leave_first)
		conn.exit_config_mode()
		reply, command = platform.finish.after_leave(conn)
		# The change is live either way; saving failing (e.g. Gaia's config
		# lock held by another session) means it's lost at the next reboot
		if command and isinstance(reply, str) and \
				(complaint := rejection(reply, command)):
			report.action_needed(device, f"the change is live but NOT saved — "
			                     f"save it on the device ({complaint})")
		return True

	def _finish_in_new_session(self) -> None:
		"""Save from a fresh session after the old one lost its prompt."""
		device, report = self._device, self._report
		try:
			outcome = self._platform.finish.in_new_session(
				lambda: netmiko.ConnectHandler(**_connect_args(device)))
			if outcome is None:
				report.notify(f"{device.endpoint}: prompt changed after the push "
				              f"(e.g. a new hostname) — applied", Tone.WARNING)
				return
			reply, command = outcome
			if isinstance(reply, str) and (complaint := rejection(reply, command)):
				raise RuntimeError(complaint)
			report.notify(f"{device.endpoint}: prompt changed after the push (e.g. "
			              f"a new hostname) — applied, and saved from a new "
			              f"session", Tone.WARNING)
		except Exception as e:
			report.action_needed(device, f"the change is live but NOT saved — "
			                     f"save it on the device (the prompt changed "
			                     f"after the push and saving from a new session "
			                     f"failed: {e})")

	def fetch_config(self) -> str | None:
		"""The running config, printed in the syntax engineers type (see
		PLATFORMS), over the same SSH as the push — the device's port and
		credentials.

		:returns: the config; None if it can't be fetched (reported in the log)"""
		device, platform, report = self._device, self._platform, self._report
		try:
			with netmiko.ConnectHandler(**_connect_args(device)) as conn:
				if problem := _enter_cli_shell(conn, platform):
					report.notify(f"could not fetch the config of {device.endpoint} "
					              f"to verify it: {problem}", Tone.ERROR)
					return None
				conn.enable()
				output = ""
				for command in platform.show_config:
					# text: no parsing is asked for (TextFSM, TTP, Genie)
					output = cast(str, conn.send_command(command,
					                                     read_timeout=FETCH_TIMEOUT))
				return output
		except Exception as e:
			report.notify(f"could not fetch the config of {device.endpoint} to "
			              f"verify it: {e}", Tone.ERROR)
			return None
