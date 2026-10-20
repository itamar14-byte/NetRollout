"""One device's SSH conversation in a rollout (Netmiko): the push - into the
CLI shell and config mode, the commands line by line with what the device
replied to each, the platform's finish - with, when verifying, the
configuration read before and after it in the same session and the push
judged by it (src/rollout/evidence.py); and the rollout's report
(RunReport), which the sessions write and the engine reads."""
from __future__ import annotations   # type hints are never evaluated

import difflib
import re
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, NamedTuple, Protocol, cast

import netmiko

from src.rollout.address import is_ip, resolve
from src.rollout.evidence import Outcome, judge, parse
from src.rollout.log import Tone, redact
from src.rollout.platforms import FETCH_TIMEOUT, PLATFORMS, ConfigSession, Platform, Target
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
		self.notify(f"ACTION NEEDED - {device.endpoint}: {what}", Tone.ERROR,
		            important=True)

	def actions(self, endpoint: str) -> str | None:
		""":returns: what a person must do on that device, a line each; None
		 when nothing"""
		return "\n".join(self.needs_action.get(endpoint, [])) or None


class PushResult(NamedTuple):
	"""How the push went on one device."""
	applied: bool    # reached the device and finished (connected, committed)
	# the device stopped answering before the last command: how many were
	# sent (the one without an answer included); the rest weren't, nothing
	# was saved
	sent: int | None = None
	interrupted: bool = False
	# per command sent: what the device replied (shown, never judged)
	replies: tuple[tuple[str, ...], ...] = ()
	# the push judged by the device's configuration; None: not verified
	# (verification off, or a snapshot couldn't be taken)
	outcome: Outcome | None = None
	diff: str | None = None    # the configuration before → after (unified)
	after: str | None = None   # the configuration after (Verify Diff)


# a line the device prints between commands that is a time stamp, not an
# answer: IOS "*Oct 19 19:17:26.223: …", NX-OS "2026 Oct 19 16:10:28 …"
_EVENT = re.compile(r"^\*?(\d{4} )?[A-Z][a-z]{2} +\d+ (\d{4} )?\d\d:\d\d:\d\d")
# Junos's context banner after a command ("[edit interfaces ge-0/0/0]")
_CONTEXT = re.compile(r"^\[edit( .*)?\]$")


def reply(output: str, command: str, prompt: str) -> tuple[list[str], list[str]]:
	"""What the device said to one command, told apart by shape from what
	isn't an answer: the command's echo, the prompt (with any context:
	"R1(config-if)#", "FGT1 (global) #"), blank lines, IOS's "^" pointer,
	Junos's "[edit …]" banner. Time-stamped lines are device events.

	:returns: (the reply's lines, the event lines) - shown, never judged"""
	said, events = [], []
	sent = command.strip()
	base = prompt.strip()
	for row in output.splitlines():
		text = row.strip()
		if not text or set(text) <= {"^"} or _CONTEXT.match(text):
			continue
		if sent and text.endswith(sent) and (text == sent or text.startswith(base)):
			continue
		if base and text.startswith(base) and text[-1:] in "#>$" and sent not in text:
			continue
		if _EVENT.match(text):
			events.append(text)
		else:
			said.append(text)
	return said, events


def _connect_args(device: Device) -> dict[str, str | int]:
	""":returns: Netmiko's ConnectHandler arguments for this device"""
	return {
		"ip": device.host,     # Netmiko takes a name here too
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
	        f"'{platform.cli_shell or '?'}' didn't leave it - set its shell to "
	        f"clish")


def _said(output: object, command: str) -> str:
	""":returns: a reply's lines on one line, its echo left out (for a log line)"""
	return " | ".join(line.strip() for line in str(output).splitlines()
	                  if line.strip() and line.strip() != command)


def _fortios_save_if_manual(conn: ConfigSession) -> str | None:
	"""FortiOS saves changes automatically unless `cfg-save` is manual (lost
	at reboot) or revert (undone after a timeout) - then save explicitly.
	With VDOMs, the setting lives under `config global`.

	:returns: the save's reply when it saved explicitly; None when the
	 device saves by itself"""
	vdoms = getattr(conn, "_vdoms", False)
	if vdoms:
		conn.send_command_timing("config global")
	mode = conn.send_command_timing("get system global | grep cfg-save")
	answer = None
	if "manual" in mode or "revert" in mode:
		answer = _said(conn.send_command_timing("execute cfg save"), "execute cfg save")
	if vdoms:
		conn.send_command_timing("end")
	return answer


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

	def push(self, commands: list[str], verify: bool = False) -> PushResult:
		"""Push the commands (resolved for this device) over SSH, then finish
		them the way the platform needs (save, commit, a command or nothing).
		Every command is sent, whatever the device answers; its answer is
		kept. With verify: the configuration is read before and after, in
		this session, and the push judged by it (evidence.judge).

		:returns: applied False when nothing took effect (no connection, a
		 failed commit)"""
		device, platform, report = self._device, self._platform, self._report
		report.notify(f"connecting to {device.endpoint}", Tone.WARNING)
		if not is_ip(device.host) and resolve(device.host) is None:
			# said as it is - Netmiko would call it a timeout or a socket error
			report.notify(f"{device.endpoint}: can't resolve {device.host} - check the "
			              f"name and the DNS this server uses", Tone.ERROR)
			return PushResult(applied=False)
		commands_sent = False
		sent = 0      # commands sent so far
		replies: list[tuple[str, ...]] = []
		before: str | None = None
		try:
			net_connect = netmiko.ConnectHandler(**_connect_args(device))
			try:
				report.notify(f"{device.ip} connected successfully", Tone.SUCCESS)
				# Goes into privileged config mode, depending on the platform
				if problem := _enter_cli_shell(net_connect, platform):
					report.action_needed(device, f"{problem} (nothing was sent)")
					return PushResult(applied=False)
				net_connect.enable()
				if verify:
					before = self._snapshot(net_connect, "before the push")
				if platform.config_command:
					try:
						net_connect.config_mode(
							config_command=platform.config_command)
					except (ValueError, netmiko.exceptions.ReadTimeout) as e:
						report.action_needed(
							device, f"'{platform.config_command}' was refused - "
							f"usually another session has uncommitted changes in "
							f"the shared configuration: commit or discard them "
							f"(they're someone else's work), then rerun. Nothing "
							f"was sent ({e})")
						return PushResult(applied=False)
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
					said, events = reply(str(output), command, str(net_connect.base_prompt))
					replies.append(tuple(said))
					if said:
						report.notify(f"{device.endpoint}: '{command.strip()}' - the "
						              f"device replied: {' | '.join(said)}", Tone.WARNING)
					for event in events:
						report.notify(f"{device.endpoint}: device event: {event}")

				if not self._finish(net_connect):
					return PushResult(applied=False, replies=tuple(replies))
				if before is None:
					return PushResult(applied=True, replies=tuple(replies))
				outcome, diff, after = self._judge(commands, before,
				                                   lambda why: self._snapshot(net_connect, why),
				                                   net_connect)
				return PushResult(applied=True, replies=tuple(replies),
				                  outcome=outcome, diff=diff, after=after)
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
			return PushResult(applied=False)
		except netmiko.NetmikoTimeoutException:
			report.notify(f"{device.ip} timed out", Tone.ERROR)
			return PushResult(applied=False)
		except netmiko.exceptions.ReadTimeout as e:
			if commands_sent and platform.finish.interruptible and \
					sent == len(commands):
				# The last command changed the prompt (e.g. a new hostname):
				# the commands were applied, but the finish couldn't run in this
				# session - a fresh one learns the new prompt (and reads the
				# configuration after, when verifying)
				replies.append(())
				self._finish_in_new_session()
				if before is None:
					return PushResult(applied=True, replies=tuple(replies))
				outcome, diff, after = self._judge(commands, before, self._fetch_new, None)
				return PushResult(applied=True, replies=tuple(replies),
				                  outcome=outcome, diff=diff, after=after)
			if commands_sent and platform.finish.interruptible:
				# No answer before the last command (a question without a
				# prompt, a slow command): the rest isn't sent, nothing saved
				answered = sent - 1
				report.action_needed(device, (
					f"stopped answering after '{commands[sent - 1].strip()}' "
					f"(command {sent} of {len(commands)}): "
					+ (f"the {answered} before it {'is' if answered == 1 else 'are'} "
					   f"live but NOT saved, " if answered else "")
					+ f"the {len(commands) - sent} after it weren't sent - check "
					  f"the device, then save the change or remove it"))
				return PushResult(applied=answered > 0, sent=sent, interrupted=True,
				                  replies=tuple(replies))
			report.notify(f"{device.ip} failed: {e}", Tone.ERROR)
			return PushResult(applied=False, replies=tuple(replies))
		except Exception as e:
			report.notify(f"{device.ip} failed: {e}", Tone.ERROR)
			return PushResult(applied=False, replies=tuple(replies))

	def _judge(self, commands: list[str], before: str,
	           snapshot: Callable[[str], str | None],
	           conn: ConfigSession | None) -> tuple[Outcome | None, str | None, str | None]:
		"""The push judged by the configuration after it (evidence.judge); one
		more snapshot only when changes no command explains appear (the
		device's own churn is told apart from someone else's change); then,
		on a platform that saves, the save checked.

		:param snapshot: reads the configuration now (the reason in its log)
		:param conn: the push's session, for the save check; None: no check
		:returns: (outcome, the before → after diff, the configuration after);
		 (None, None, None) when the after snapshot couldn't be taken"""
		platform = self._platform
		after = snapshot("after the push")
		if after is None:
			return None, None, None
		outcome = judge(commands, before, after, flat=platform.flat,
		                blocks=platform.close_blocks)
		if outcome.other_changes:
			again = snapshot("again, to tell the device's own changes apart")
			if again is not None:
				outcome = judge(commands, before, after, flat=platform.flat,
				                blocks=platform.close_blocks, again=again)
		if conn is not None:
			self._check_saved(conn, outcome)
		diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
		                                      "before", "after", n=3, lineterm=""))
		return outcome, diff, after

	def _snapshot(self, conn: ConfigSession, why: str) -> str | None:
		""":returns: the configuration now, without its framing; None if it
		 can't be read (said in the log: the device is then not verified)"""
		device, platform = self._device, self._platform
		try:
			output = ""
			for command in platform.show_config:
				# text: no parsing is asked for (TextFSM, TTP, Genie)
				output = cast(str, conn.send_command(command, read_timeout=FETCH_TIMEOUT))
			return platform.config(output)
		except Exception as e:
			self._report.notify(f"{device.endpoint}: couldn't read the configuration "
			                    f"{why} - not verified ({e})", Tone.ERROR)
			return None

	def _check_saved(self, conn: ConfigSession, outcome: Outcome) -> None:
		"""On a platform that shows its saved configuration: every line the
		push added is in it and every line it removed isn't - else the change
		is live but would be lost at the next reboot."""
		platform, device = self._platform, self._device
		if not platform.show_saved or not (outcome.added or outcome.removed):
			return
		try:
			saved = Counter(line.key for line in parse(
				cast(str, conn.send_command(platform.show_saved, read_timeout=FETCH_TIMEOUT)),
				platform.flat))
		except Exception as e:
			self._report.notify(f"{device.endpoint}: couldn't read the saved configuration "
			                    f"to check the save ({e})", Tone.ERROR)
			return
		missing = [text for path, text in outcome.added if not saved[(path, text)]]
		kept = [text for path, text in outcome.removed if saved[(path, text)]]
		if missing or kept:
			lines = [f"'{text}' missing" for text in missing[:5]] + \
				[f"'{text}' still there" for text in kept[:5]]
			self._report.action_needed(device, f"the change is live but NOT saved - the "
			                           f"saved configuration differs: {', '.join(lines)}. "
			                           f"Save it on the device")

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
				conn.send_command_timing("end")
			answer = _fortios_save_if_manual(conn)
			if answer is not None:
				report.notify(f"{device.endpoint}: cfg-save is not automatic - saved with "
				              f"'execute cfg save'" + (f" (the device replied: {answer})"
				                                       if answer else ""), Tone.WARNING)
		# Before leaving config mode: leaving discards uncommitted changes
		if not platform.finish.before_leave(conn, report, device):
			return False
		if platform.leave_first:
			conn.send_command_timing(platform.leave_first)
		conn.exit_config_mode()
		answer, command = platform.finish.after_leave(conn)
		if command and (said := _said(answer, command)):
			report.notify(f"{device.endpoint}: '{command}' - the device replied: {said}")
		if platform.saved_state:
			ask, saved = platform.saved_state
			state = str(conn.send_command(ask)).strip()
			if state.lower() != saved:
				# the change is live either way; unsaved, it's lost at the next
				# reboot (Gaia: the config lock held by another session)
				report.action_needed(device, f"the change is live but NOT saved - "
				                     f"'{ask}' answers {state!r}" + (f" after '{command}' replied: {said}"
				                                                     if command and said else "")
				                     + " - save it on the device")
		return True

	def _finish_in_new_session(self) -> None:
		"""Save from a fresh session after the old one lost its prompt."""
		device, report = self._device, self._report
		try:
			outcome = self._platform.finish.in_new_session(
				lambda: netmiko.ConnectHandler(**_connect_args(device)))
			if outcome is None:
				report.notify(f"{device.endpoint}: prompt changed after the push (e.g. "
				              f"a new hostname)", Tone.WARNING)
				return
			answer, command = outcome
			said = _said(answer, command)
			report.notify(f"{device.endpoint}: prompt changed after the push (e.g. a new "
			              f"hostname) - '{command}' sent from a new session"
			              + (f" (the device replied: {said})" if said else ""), Tone.WARNING)
		except Exception as e:
			report.action_needed(device, f"the change is live but NOT saved - "
			                     f"save it on the device (the prompt changed "
			                     f"after the push and saving from a new session "
			                     f"failed: {e})")

	def _fetch_new(self, why: str) -> str | None:
		"""The configuration now, from a new session (the push's own lost its
		prompt)."""
		device, platform = self._device, self._platform
		try:
			with netmiko.ConnectHandler(**_connect_args(device)) as conn:
				if problem := _enter_cli_shell(conn, platform):
					raise RuntimeError(problem)
				conn.enable()
				return self._snapshot(conn, why)
		except Exception as e:
			self._report.notify(f"{device.endpoint}: couldn't read the configuration "
			                    f"{why} - not verified ({e})", Tone.ERROR)
			return None
