"""One device's SSH conversation in a rollout (Netmiko): the push - into the
CLI shell and config mode, the commands line by line with what the device
replied to each, the platform's finish - with, when verifying, the
configuration read before and after it in the same session and the push
judged by it (src/rollout/evidence.py); and the rollout's report
(RunReport), which the sessions write and the engine reads."""
from __future__ import annotations   # type hints are never evaluated

import difflib
import io
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import netmiko

from src.rollout.address import is_ip, resolve
from src.rollout.evidence import Outcome, judge, parse
from src.rollout.log import Tone, redact
from src.rollout.platforms import (
	COMMIT_TIMEOUT, DEFAULTS_MAX_LINES, PLATFORMS, CommitPending, ConfigSession, EnableFailed, Platform, Target,
	one_line, reply)
if TYPE_CHECKING:   # annotations only: engine.py imports this module
	from src.rollout.engine import Device



def first_line(error: BaseException) -> str:
	""":returns: an error's first line - Netmiko's add a paragraph of advice
	 below it, which would break the log's one line per event"""
	lines = [line.strip() for line in str(error).splitlines() if line.strip()]
	return lines[0] if lines else type(error).__name__


def first_sentence(error: BaseException) -> str:
	""":returns: an error's first sentence (Netmiko's enable() failure goes on
	 with "Please ensure you pass the 'secret' argument…")"""
	line = first_line(error)
	end = line.find(". ")
	return line[:end + 1] if end != -1 else line


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


# a commit that timed out: how many times the configuration is read (a new
# session each) to see whether it went through, and how far apart
COMMIT_CHECKS = 5
COMMIT_CHECK_EVERY = 30


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
	after: str | None = None   # the configuration after (Results → Verification)


def _clear(conn: ConfigSession, channel: io.BytesIO) -> None:
	"""Empty the session's record before the next command (it keeps only
	what one command printed)."""
	log = getattr(conn, "session_log", None)
	if log is not None:
		log.flush()
	channel.seek(0)
	channel.truncate()


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


@contextmanager
def _new_session(device: Device, platform: Platform) -> Iterator[ConfigSession]:
	""":returns: a new session to the device, ready for commands
	 (Platform.prepare)
	:raises RuntimeError: it couldn't be made ready"""
	with netmiko.ConnectHandler(**_connect_args(device)) as conn:
		if problem := platform.prepare(conn):
			raise RuntimeError(problem)
		yield conn


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
		# what the device printed, kept in memory (Netmiko leaves passwords
		# out): a command that gets no prompt back loses its output with the
		# timeout - this keeps it (a question such as "[confirm]")
		channel = io.BytesIO()
		try:
			net_connect = netmiko.ConnectHandler(**_connect_args(device), session_log=channel)
			try:
				report.notify(f"{device.ip} connected successfully", Tone.SUCCESS)
				# the session brought to speak the platform's language
				if problem := platform.prepare(net_connect):
					report.action_needed(device, f"{problem} (nothing was sent)")
					return PushResult(applied=False)
				if verify:
					before = self._snapshot(net_connect, "before the push")
				if problem := platform.enter_config(net_connect):
					report.action_needed(device, problem)
					return PushResult(applied=False)

				for command in commands:
					commands_sent = True
					sent += 1
					# Config mode was entered above: each command goes in exactly
					# as typed. Netmiko's default re-checks config mode per call,
					# and drivers that only recognise their top-level config
					# prompt (Aruba CX: "(config)#") then fail inside a section
					_clear(net_connect, channel)
					try:
						output = net_connect.send_config_set(
							[command.strip()], enter_config_mode=False,
							exit_config_mode=False)
					except netmiko.exceptions.ReadTimeout:
						replies.append(tuple(self._last_output(net_connect, channel, command)))
						raise
					said, events = reply(str(output), command, str(net_connect.base_prompt),
					                     self._platform.events)
					replies.append(tuple(said))
					if said:
						# without verify nothing judges the command: its reply is
						# all there is to see, so it's shown on the console too
						report.notify(f"{device.endpoint}: '{command.strip()}' - the "
						              f"device replied: {' | '.join(said)}", Tone.WARNING,
						              important=not verify)
					for event in events:
						report.notify(f"{device.endpoint}: device event: {event}")
				# without verbose the commands aren't logged one by one: this line
				# keeps the order readable (pushed, then saved, then judged)
				report.notify(f"{device.endpoint}: pushed {sent} command{'s' if sent != 1 else ''}")

				try:
					if not self._finish(net_connect):
						return PushResult(applied=False, replies=tuple(replies))
				except CommitPending:
					return self._after_slow_commit(commands, before, tuple(replies))
				if before is None:
					return PushResult(applied=True, replies=tuple(replies))
				outcome, diff, after = self._judge(commands, before, tuple(replies),
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
		except EnableFailed as e:
			# "secret" last: the log's redaction hides the word after it
			report.notify(f"{device.ip} couldn't enter enable mode ({first_sentence(e)}) - "
			              f"check the security profile's enable secret", Tone.ERROR)
			return PushResult(applied=False)
		except netmiko.exceptions.ReadTimeout as e:
			if commands_sent and platform.finish.interruptible and \
					sent == len(commands):
				# The last command changed the prompt (e.g. a new hostname):
				# the commands were applied, but the finish couldn't run in this
				# session - a fresh one learns the new prompt (and reads the
				# configuration after, when verifying)
				self._finish_in_new_session()
				if before is None:
					return PushResult(applied=True, replies=tuple(replies))
				outcome, diff, after = self._judge(commands, before, tuple(replies),
				                                   self._fetch_new, None)
				return PushResult(applied=True, replies=tuple(replies),
				                  outcome=outcome, diff=diff, after=after)
			if commands_sent and platform.finish.interruptible:
				# No answer before the last command (a question without a
				# prompt, a slow command): the rest isn't sent, nothing saved
				answered = sent - 1
				last = " | ".join(replies[-1]) if replies and replies[-1] else ""
				report.action_needed(device, (
					f"stopped answering after '{commands[sent - 1].strip()}' "
					f"(command {sent} of {len(commands)}"
					+ (f"; its last output: {last}" if last else "") + "): "
					+ (f"the {answered} before it {'is' if answered == 1 else 'are'} "
					   f"live but NOT saved, " if answered else "")
					+ f"the {len(commands) - sent} after it weren't sent - check "
					  f"the device, then save the change or remove it"))
				return PushResult(applied=answered > 0, sent=sent, interrupted=True,
				                  replies=tuple(replies))
			report.notify(f"{device.ip} failed: {first_line(e)}", Tone.ERROR)
			return PushResult(applied=False, replies=tuple(replies))
		except Exception as e:
			report.notify(f"{device.ip} failed: {first_line(e)}", Tone.ERROR)
			return PushResult(applied=False, replies=tuple(replies))

	def _last_output(self, conn: ConfigSession, channel: io.BytesIO,
	                 command: str) -> list[str]:
		"""What the device printed to a command that got no prompt back (a
		question, a slow command), from the session's own record - logged.

		:returns: its lines, the echo and prompt left out"""
		log = getattr(conn, "session_log", None)
		if log is not None:
			log.flush()
		said, _ = reply(channel.getvalue().decode("utf-8", "replace"), command,
		                str(getattr(conn, "base_prompt", "")), self._platform.events)
		if said:
			self._report.notify(f"{self._device.endpoint}: '{command.strip()}' - no prompt "
			                    f"came back; the device's last output: {' | '.join(said)}",
			                    Tone.WARNING)
		return said

	def _after_slow_commit(self, commands: list[str], before: str | None,
	                       replies: tuple[tuple[str, ...], ...]) -> PushResult:
		"""A commit that didn't answer in time, decided by the configuration
		read from new sessions: changed → it went through (judged as usual);
		unchanged after COMMIT_CHECKS reads → failed, a person checks (it may
		still be running). Without verify there is nothing to compare with.

		:param before: the configuration before the push; None: not verifying"""
		device, report = self._device, self._report
		waited = f"the commit didn't finish within {COMMIT_TIMEOUT}s"
		if before is None:
			report.action_needed(device, f"{waited} - check on the device whether "
			                     f"it went through (reported as failed)")
			return PushResult(applied=False, replies=replies)
		flat, navigation = self._platform.flat, self._platform.navigation
		was = [line.key for line in parse(before, flat, navigation)]
		for check in range(COMMIT_CHECKS):
			if check:
				time.sleep(COMMIT_CHECK_EVERY)
			after = self._fetch_new("after the slow commit")
			if after is None:
				break
			if [line.key for line in parse(after, flat, navigation)] != was:
				report.notify(f"{device.endpoint}: {waited}, and the configuration has "
				              f"changed since - judged by it", Tone.WARNING)
				first = [after]
				outcome, diff, after = self._judge(
					commands, before, replies,
					lambda why: first.pop() if first else self._fetch_new(why), None)
				return PushResult(applied=True, replies=replies, outcome=outcome,
				                  diff=diff, after=after)
		else:
			report.action_needed(device, f"{waited} and the configuration still hadn't "
			                     f"changed {(COMMIT_CHECKS - 1) * COMMIT_CHECK_EVERY}s later - "
			                     f"it failed or is still running: check on the device "
			                     f"(reported as failed)")
			return PushResult(applied=False, replies=replies)
		report.action_needed(device, f"{waited} - check on the device whether it went "
		                     f"through (reported as failed)")
		return PushResult(applied=False, replies=replies)

	def _judge(self, commands: list[str], before: str,
	           replies: tuple[tuple[str, ...], ...],
	           snapshot: Callable[[str], str | None],
	           conn: ConfigSession | None) -> tuple[Outcome | None, str | None, str | None]:
		"""The push judged by the configuration after it (evidence.judge); one
		more snapshot only when changes no command explains appear (the
		device's own churn is told apart from someone else's change); the
		configuration with its defaults only when commands are left unproven
		and the platform can show it; then, on a platform that saves, the
		save checked.

		:param replies: what the device said to each command (whether it said
		 anything counts)
		:param snapshot: reads the configuration now (the reason in its log)
		:param conn: the push's session, for the save check and the defaults;
		 None: from a new session, no save check
		:returns: (outcome, the before → after diff, the configuration after);
		 (None, None, None) when the after snapshot couldn't be taken"""
		platform = self._platform
		after = snapshot("after the push")
		if after is None:
			return None, None, None
		grammar: dict[str, Any] = {**platform.grammar, "replied": [bool(r) for r in replies]}
		outcome = judge(commands, before, after, **grammar)
		if outcome.other_changes:
			again = snapshot("again, to tell the device's own changes apart")
			if again is not None:
				grammar["again"] = again
				outcome = judge(commands, before, after, **grammar)
		fetches = platform.defaults(outcome.unproven) if outcome.unproven else []
		if fetches:
			view = self._defaults(conn, fetches)
			if view is not None:
				outcome = judge(commands, before, after, defaults=view, **grammar)
		if conn is not None:
			self._check_saved(conn, outcome)
		diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
		                                      "before", "after", n=3, lineterm=""))
		return outcome, diff, after

	def _defaults(self, conn: ConfigSession | None, fetches: list[str]) -> str | None:
		"""The configuration shown with its defaults (Platform.defaults), for
		commands the plain one didn't prove - in the push's session, else a
		new one. Logged; when it can't be read, or is too long to judge
		(DEFAULTS_MAX_LINES), those commands stay as they are.

		:returns: the views, one after the other; None: not read"""
		device, platform = self._device, self._platform

		try:
			if conn is not None:
				view = platform.read_defaults(conn, fetches)
			else:
				with _new_session(device, platform) as new:
					view = platform.read_defaults(new, fetches)
		except Exception as e:
			self._report.notify(f"{device.endpoint}: couldn't read the configuration with "
			                    f"its defaults ({e}) - unproven commands stay so", Tone.WARNING)
			return None
		if len(view.splitlines()) > DEFAULTS_MAX_LINES:
			self._report.notify(f"{device.endpoint}: the configuration with its defaults is "
			                    f"over {DEFAULTS_MAX_LINES} lines - not judged", Tone.WARNING)
			return None
		self._report.notify(f"{device.endpoint}: read the configuration with its defaults "
		                    f"for the unproven commands ({' / '.join(fetches)})")
		return view

	def _snapshot(self, conn: ConfigSession, why: str) -> str | None:
		""":returns: the configuration now, without its framing; None if it
		 can't be read (said in the log: the device is then not verified)"""
		device, platform = self._device, self._platform
		try:
			return platform.read_config(conn)
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
			saved = Counter(line.key for line in parse(platform.read_saved(conn), platform.flat,
			                                           platform.navigation))
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
		"""Finish a pushed device the way its platform needs: what it needs
		done in config mode first (FortiOS: close blocks, save when cfg-save
		isn't automatic), the Finish around leaving config mode, then its
		save state where it tells one.

		:param conn: the push's open session
		:returns: False if the change didn't take effect (a failed commit)"""
		device, platform, report = self._device, self._platform, self._report
		platform.before_finish(conn, report, device)
		# Before leaving config mode: leaving discards uncommitted changes
		if not platform.finish.before_leave(conn, report, device):
			return False
		platform.leave_config(conn)
		answer, command = platform.finish.after_leave(conn)
		said = one_line(answer, command) if command else ""
		if said:
			report.notify(f"{device.endpoint}: '{command}' - the device replied: {said}")
		if unsaved := platform.unsaved(conn):
			# the change is live either way; unsaved, it's lost at the next
			# reboot (Gaia: the config lock held by another session)
			ask, state = unsaved
			report.action_needed(device, f"the change is live but NOT saved - "
			                     f"'{ask}' answers {state!r}" + (f" after '{command}' replied: {said}"
			                                                     if said else "")
			                     + " - save it on the device")
		return True

	def _finish_in_new_session(self) -> None:
		"""Save from a fresh session after the old one lost its prompt."""
		device, report = self._device, self._report
		try:
			outcome = self._platform.finish.in_new_session(
				lambda: _new_session(device, self._platform))
			if outcome is None:
				report.notify(f"{device.endpoint}: prompt changed after the push (e.g. "
				              f"a new hostname)", Tone.WARNING)
				return
			answer, command = outcome
			said = one_line(answer, command)
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
			with _new_session(device, platform) as conn:
				return self._snapshot(conn, why)
		except Exception as e:
			self._report.notify(f"{device.endpoint}: couldn't read the configuration "
			                    f"{why} - not verified ({e})", Tone.ERROR)
			return None
