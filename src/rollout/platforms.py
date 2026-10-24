"""What NetRollout knows about each platform (Netmiko device type), with no
I/O of its own: its grammar - how a push finishes (the Finish family: save /
commit / a command / nothing - acting on the session it is handed), how its
config prints and is read back (PLATFORMS), what frames that output. Never
what its messages mean: a push is judged by the device's own configuration
(src/rollout/evidence.py); what a device says is told apart from its echo
and prompt by shape (reply), shown, never judged. The SSH conversation is
src/rollout/session.py's.
Adding a vendor: one PLATFORMS row plus a fixture in
tests/unit/rollout/test_platforms.py."""
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from netmiko.exceptions import ReadTimeout

from src.rollout.evidence import NAVIGATION, NEGATION
from src.rollout.log import Tone


# A line the device prints between commands that is a logged event, not an
# answer - told apart by its shape: a time stamp first (IOS "*Oct 19
# 19:17:26.223: …" - "." instead of "*" when the clock isn't synced -, NX-OS
# / EOS "2026 Oct 19 16:10:28 …", ISO "2026-10-19T…"), or an uptime stamp
# before a syslog message (IOS "*00:12:34: %LINK-3-…", "1w2d: %…"), or a
# syslog message itself ("%LINK-3-UPDOWN: …": facility, severity digit,
# mnemonic) - any of them after IOS's sequence number ("000123: "); or a
# message to every terminal ("Message from syslogd@…" - Junos, Linux-based
# CLIs; "Broadcast message from …" - Gaia). Per-platform shapes extend it
# (PLATFORMS' events).
_TIME_STAMP = r"[*.]?(\d{4} )?[A-Z][a-z]{2} +\d+ (\d{4} )?\d\d:\d\d:\d\d"
_ISO_STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d"
_UPTIME_SYSLOG = r"[*.]?(\d+:\d\d:\d\d|\d+[ywdh]\d+[wdhm])(\.\d+)?: +%"
_SYSLOG = r"%[A-Z][A-Z0-9_]*-\d-[A-Z0-9_]+:"
_SHAPES = (rf"(\d+: )?({_TIME_STAMP}|{_ISO_STAMP}|{_UPTIME_SYSLOG}|{_SYSLOG})"
           r"|Message from syslogd@|Broadcast message from ")
EVENTS = re.compile(rf"^({_SHAPES})")
# Comware "%Oct 21 10:00:00:123 2026 HP IFNET/3/PHY_UPDOWN: …" (a trap "#…",
# debugging "*…"; before the clock is set "%0.109391473 HP …"); ProCurve
# "I 10/21/26 10:00:00 00076 ports: …"; IOS-XR "RP/0/RP0/CPU0:Oct 21
# 10:00:00…" (with "service timestamps … year", the year first)
_COMWARE_EVENTS = re.compile(rf"^({_SHAPES}|[%#*][A-Z][a-z]{{2}} +\d+ \d\d:\d\d:\d\d|%\d+\.\d+ )")
_PROCURVE_EVENTS = re.compile(rf"^({_SHAPES}|[IWMDE] \d\d/\d\d/\d\d \d\d:\d\d:\d\d)")
_XR_EVENTS = re.compile(rf"^({_SHAPES}|[A-Z]+/\d+/[^:\s]+:(\d{{4}} )?[A-Z][a-z]{{2}} +\d+ )")
# A progress bar's frame, redrawn many times while a device works (NX-OS's
# copy running-config startup-config: "[######          ]  41%") - its shape:
# only bar characters and a percentage; not an answer, not worth a log line
PROGRESS = re.compile(r"^\[[#=*>.\s-]*\]\s*\d{1,3}%(\s*\|?\s*\[[#=*>.\s-]*\]\s*\d{1,3}%)*$")
# Junos's context banner after a command ("[edit interfaces ge-0/0/0]")
_CONTEXT = re.compile(r"^\[edit( .*)?\]$")


def reply(output: str, command: str, prompt: str,
          shapes: re.Pattern[str] = EVENTS) -> tuple[list[str], list[str]]:
	"""What the device said to one command, told apart by shape from what
	isn't an answer: the command's echo, the prompt (with any context:
	"R1(config-if)#", "FGT1 (global) #"), blank lines, IOS's "^" pointer,
	Junos's "[edit …]" banner. Lines of the platform's event shapes are
	device events.

	:param shapes: the platform's event shapes (Platform.events)
	:returns: (the reply's lines, the event lines) - whether there is a
	 reply counts in the judgment, its wording never"""
	said, events = [], []
	sent = command.strip()
	base = prompt.strip()
	for row in output.splitlines():
		text = row.strip()
		if not text or set(text) <= {"^"} or _CONTEXT.match(text) or PROGRESS.match(text):
			continue
		# Comware's prompts are bracketed ("[HP-GigabitEthernet1/0/1]", "<HP>")
		bare = text.lstrip("[<")
		if sent and text.endswith(sent) and (text == sent or bare.startswith(base)):
			continue
		if base and bare.startswith(base) and text[-1:] in "#>$]" and sent not in text:
			continue
		if shapes.match(text):
			events.append(text)
		else:
			said.append(text)
	return said, events


class ConfigSession(Protocol):
	"""The Netmiko connection methods a push, its finish and a config fetch
	use (netmiko.BaseConnection has them all)."""

	def find_prompt(self) -> str: ...

	def set_base_prompt(self) -> str: ...

	def enable(self) -> str: ...

	def config_mode(self, config_command: str = "") -> str: ...

	def exit_config_mode(self) -> str: ...

	def send_config_set(self, config_commands: list[str], *,
	                    exit_config_mode: bool = True,
	                    enter_config_mode: bool = True,
	                    cmd_verify: bool = True) -> str: ...

	def send_command(self, command_string: str, *, expect_string: str | None = None,
	                 read_timeout: float = 10.0) -> Any: ...

	def send_command_timing(self, command_string: str) -> Any: ...

	def read_channel(self) -> str: ...

	# each committing driver has its own options (Junos, IOS XR and PAN-OS
	# all take read_timeout); netmiko's base class declares none
	@property
	def commit(self) -> Callable[..., str]: ...

	def save_config(self) -> str: ...

	def disconnect(self) -> None: ...


class Target(Protocol):
	"""What a finish names a device by (a rollout Device)."""

	@property
	def endpoint(self) -> str: ...


class Report(Protocol):
	"""Where a finish reports (the rollout's RunReport, src/rollout/session.py)."""

	def notify(self, text: str, tone: Tone, important: bool = False) -> None: ...

	def action_needed(self, device: Target, what: str) -> None: ...


class Finish(ABC):
	"""How a pushed change takes effect and is kept, once the commands are
	in: around leaving config mode (before_leave, after_leave), and from a
	fresh session when the push's own lost its prompt (in_new_session)."""

	@property
	def interruptible(self) -> bool:
		""":returns: whether a device that stops answering mid-push keeps
		 what it took (live, unsaved) - True unless a commit would have made
		 it take effect"""
		return True

	def before_leave(self, conn: ConfigSession, report: Report,
	                 device: Target) -> bool:
		"""Runs in config mode, after the commands.

		:returns: False if the change didn't take effect (config mode is
		 then left already)"""
		return True

	@abstractmethod
	def after_leave(self, conn: ConfigSession) -> tuple[Any, str]:
		"""Runs after leaving config mode.

		:returns: (the device's reply, the command it answers - "" when
		 nothing was sent)"""

	def in_new_session(self, connect: Callable[
			[], AbstractContextManager[ConfigSession]]) -> tuple[Any, str] | None:
		"""Finish from a fresh session (out of config mode).

		:param connect: opens it, ready for commands (privileged where the
		 platform has an enable step)
		:returns: as after_leave; None when there is nothing to finish (no
		 session opened)"""
		with connect() as conn:
			return self.after_leave(conn)


class SaveConfig(Finish):
	"""save_config() after leaving config mode."""

	def after_leave(self, conn: ConfigSession) -> tuple[Any, str]:
		return conn.save_config(), "save"


class RunCommand(Finish):
	"""A CLI command after leaving config mode (Gaia's `save config`)."""

	def __init__(self, text: str) -> None:
		self.text = text

	def after_leave(self, conn: ConfigSession) -> tuple[Any, str]:
		return conn.send_command(self.text), self.text


class NoFinish(Finish):
	"""Nothing: the change is live as typed."""

	def after_leave(self, conn: ConfigSession) -> tuple[Any, str]:
		return "", ""

	def in_new_session(self, connect: Callable[
			[], AbstractContextManager[ConfigSession]]) -> tuple[Any, str] | None:
		return None


class CommitPending(Exception):
	"""The commit didn't answer within COMMIT_TIMEOUT: it may still be
	running on the device. Nothing is claimed from the timeout - the session
	decides by the configuration."""


class Commit(Finish):
	"""commit() before leaving config mode (leaving discards uncommitted
	changes).

	:param discard: after a failed commit, the commands that drop the
	 candidate - all sent in order (whether one was accepted isn't judged
	 from its wording: the device's replies are shown)
	:param pending: the command that shows what is still in the candidate
	 (Junos `show | compare`): no output means nothing is left - a state
	 read, not a message interpreted; "" - a person checks. Asked before the
	 commit too: a candidate with nothing in it (every command refused, or
	 none changed anything) isn't committed - PAN-OS's commit() then waits
	 for a progress bar that never comes
	:param reverts_on_failure: the platform drops a failed commit by itself
	 (IOS XR): no discard needed, nothing left to check
	:param by_pending: commit by sending "commit" and waiting for the prompt,
	 then decide by the pending command - empty: committed; the change still
	 there: the commit failed, its reply the reason (PAN-OS: Netmiko's
	 commit() waits for a "100%" a failed commit never prints)"""

	def __init__(self, discard: tuple[str, ...] = (), pending: str = "",
	             reverts_on_failure: bool = False, by_pending: bool = False) -> None:
		self.discard = discard
		self.pending = pending
		self.reverts_on_failure = reverts_on_failure
		self.by_pending = by_pending

	@property
	def interruptible(self) -> bool:
		return False

	def before_leave(self, conn: ConfigSession, report: Report,
	                 device: Target) -> bool:
		if self._left(conn, str(getattr(conn, "base_prompt", ""))) == []:
			report.notify(f"{device.endpoint}: nothing to commit - '{self.pending}' shows "
			              f"no change in the candidate", Tone.INFO)
			return True
		try:
			if self.by_pending:
				self._commit_by_pending(conn)
			else:
				conn.commit(read_timeout=COMMIT_TIMEOUT)
		except ReadTimeout:
			# It may still complete on the device: the configuration decides
			raise CommitPending from None
		except ValueError as e:
			report.notify(f"{device.endpoint}: commit failed - nothing was "
			              f"applied. {e}", Tone.ERROR)
			if self.discard:
				prompt = str(getattr(conn, "base_prompt", ""))
				replies = []
				for command in self.discard:
					said, _ = reply(str(conn.send_config_set([command], exit_config_mode=False)),
					                command, prompt)
					replies.append(f"'{command}'" + (f" (the device replied: {' '.join(said)})"
					                                 if said else ""))
				discarded = (f"the commit failed and the failed changes were discarded "
				             f"with {', then '.join(replies)}")
				left = self._left(conn, prompt)
				if left is None:
					report.action_needed(device, f"{discarded} - check that the candidate "
					                     f"configuration is empty")
				elif left:
					report.action_needed(device, f"{discarded}, but '{self.pending}' still "
					                     f"shows: {' | '.join(left[:10])} - check the candidate "
					                     f"configuration on the device")
				else:
					report.notify(f"{device.endpoint}: {discarded}; '{self.pending}' shows "
					              f"nothing left in the candidate", Tone.WARNING)
			elif not self.reverts_on_failure:
				report.action_needed(device, "the failed changes may still be "
				                     "in the candidate configuration - discard "
				                     "them on the device")
			conn.exit_config_mode()
			return False
		return True

	def after_leave(self, conn: ConfigSession) -> tuple[Any, str]:
		return "", ""

	def _commit_by_pending(self, conn: ConfigSession) -> None:
		"""Send "commit", wait for the prompt, then read the candidate.

		:raises ValueError: the change is still pending - the commit failed
		 (the message: what the device replied to it)
		:raises ReadTimeout: no prompt within the commit timeout
		:raises CommitPending: the candidate couldn't be read after it"""
		prompt = str(getattr(conn, "base_prompt", ""))
		answer = conn.send_command("commit", expect_string=re.escape(prompt) + r"[#>]",
		                           read_timeout=COMMIT_TIMEOUT)
		left = self._left(conn, prompt)
		if left is None:
			raise CommitPending
		if left:
			raise ValueError(one_line(answer, "commit") or
			                 f"'{self.pending}' still shows the change after the commit")

	def _left(self, conn: ConfigSession, prompt: str) -> list[str] | None:
		""":returns: what the candidate still holds after the discard (its
		 lines; [] - nothing); None when the platform has no such command or
		 it couldn't be sent"""
		if not self.pending:
			return None
		try:
			output = conn.send_config_set([self.pending], exit_config_mode=False)
		except Exception:
			return None
		said, _ = reply(str(output), self.pending, prompt)
		return said


# A section as the device printed its headers ("interface Loopback95";
# FortiOS "config firewall address", 'edit "nr def"'); () at the top
Section = tuple[str, ...]
# The commands that print the configuration with the values left at their
# defaults - which the plain configuration hides (FortiOS "set admintimeout
# 5", NX-OS "no shutdown") - for these sections; [] when the platform has no
# way. Asked only for the sections of commands the plain configuration
# didn't prove (evidence.Outcome.unproven)
DefaultsFetch = Callable[[Iterable[Section]], list[str]]
# What a defaults fetch may print and still be judged (lines)
DEFAULTS_MAX_LINES = 50_000


def no_defaults(sections: Iterable[Section]) -> list[str]:
	"""No documented or verified way: those commands stay in review."""
	return []


def whole_config(command: str) -> DefaultsFetch:
	""":returns: one command printing the whole configuration with defaults
	 (IOS "show running-config all"), whatever the sections"""
	return lambda sections: [command]


def fortios_blocks(sections: Iterable[Section]) -> list[str]:
	"""FortiOS: "show full-configuration <path>" per config block - the
	whole one is tens of thousands of lines. The path from the block's
	"config …" and "edit …" headers as printed (names keep their quotes); a
	deeper section, or one under "config vdom" / "config global", has none."""
	fetches: list[str] = []
	for path in sections:
		if not path or len(path) > 2:
			continue
		head = path[0].split(None, 1)
		if len(head) < 2 or head[0].lower() != "config" or head[1].lower().split()[0] in ("vdom", "global"):
			continue
		command = f"show full-configuration {head[1]}"
		if len(path) == 2:
			entry = path[1].split(None, 1)
			if len(entry) < 2 or entry[0].lower() != "edit":
				continue
			command += f" {entry[1]}"
		if command not in fetches:
			fetches.append(command)
	return fetches


# The grammar table's columns (Platform.row; the user guide and the design
# report show it)
COLUMNS = ("read", "left_out", "saved", "layout", "navigation", "removal", "exec", "defaults", "events")


def one_line(output: object, command: str) -> str:
	""":returns: a reply's lines on one line, its echo and progress-bar frames
	 left out (for a log line)"""
	return " | ".join(line.strip() for line in str(output).splitlines()
	                  if line.strip() and line.strip() != command and not PROGRESS.match(line.strip()))


class EnableFailed(Exception):
	"""Netmiko's enable() didn't reach privileged mode - a wrong or missing
	enable secret. Netmiko has no exception of its own for it: a wrong secret
	makes IOS ask "Password:" again, so no prompt comes (ReadTimeout); a
	prompt that comes back unprivileged is its ValueError. Its message is
	Netmiko's."""


@dataclass(frozen=True)
class Platform:
	"""One platform's whole dialect: its grammar (how its configuration is
	laid out and read, which words navigate, remove or run exec commands,
	what its log lines look like, how it shows defaults) and how a session
	is brought to speak it - the shell, privilege and modes (prepare,
	read_config, enter_config, leave_config), the finish. Never what its
	messages mean. The base is the common case; a platform that differs
	overrides only that (Gaia, Junos, AosCx, FortiOS). Its methods act on
	the session they are handed - no I/O of their own."""
	finish: Finish
	show_config: tuple[str, ...] = ("show running-config",)
	flat: bool = False         # one "set …" line per setting, no sections
	close_blocks: bool = False  # FortiOS: an open config block is discarded
	# the words that leave or close a section, and that remove (evidence's
	# NAVIGATION / NEGATION are every platform's together)
	navigation: frozenset[str] = frozenset(NAVIGATION)
	negation: frozenset[str] = frozenset(NEGATION)
	# lines show_config prints that aren't the configuration and change by
	# themselves (IOS "Current configuration : 1290 bytes", IOS-XR's time
	# stamp above every show's output): a line matching it is left out
	framing: re.Pattern[str] | None = None
	# the saved configuration, to check a save kept the change; "" when the
	# platform has none to show (a commit is the save; FortiOS saves itself)
	show_saved: str = ""
	# first words that run an exec command from config mode (IOS "do show
	# …", FortiOS "get …"): not configuration - shown, never judged
	exec_words: tuple[str, ...] = ()
	# how the configuration prints with its defaults (DefaultsFetch)
	defaults: DefaultsFetch = no_defaults
	# the shapes of a logged event line (EVENTS and the platform's own)
	events: re.Pattern[str] = EVENTS
	# whether Netmiko waits for each command's exact echo before its answer:
	# a device that echoes a refused command in pieces (Junos stops at the
	# bad word, then redraws the line) never gives it - the read then times
	# out, though the device answered and prompted
	echo_whole: bool = True
	# section 5's row (row()): what can't be read off the fields above, in
	# words (read, left_out, saved, events; any column may be said
	# differently), and where each column's fact comes from - "lab"
	# (verified on a device), "doc" (documented), "inferred", "unverified"
	about: dict[str, str] = field(default_factory=dict)
	sources: dict[str, str] = field(default_factory=dict)

	def row(self) -> dict[str, str]:
		""":returns: the platform's row in the grammar table (COLUMNS), in
		 words - each column also has its source in `sources`"""
		layout = ("flat set lines" if self.flat else "config / edit blocks" if self.close_blocks
		          else "indented sections")
		derived = {"read": " / ".join(self.show_config), "layout": layout,
		           "navigation": ", ".join(sorted(self.navigation)) or "none (no configuration mode)",
		           "removal": ", ".join(sorted(self.negation)), "exec": ", ".join(self.exec_words),
		           "defaults": " / ".join(self.defaults([()])) or "none"}
		return {column: self.about.get(column, derived.get(column, "")) for column in COLUMNS}

	@property
	def grammar(self) -> dict[str, Any]:
		""":returns: the judge's grammar arguments for this platform
		 (evidence.judge: flat, blocks, exec words, navigation, removals)"""
		return {"flat": self.flat, "blocks": self.close_blocks, "exec_words": self.exec_words,
		        "navigation": self.navigation, "negation": self.negation}

	def config(self, output: str) -> str:
		""":returns: show_config's output without its framing lines"""
		if self.framing is None:
			return output
		framing = self.framing
		return "\n".join(line for line in output.splitlines()
		                 if not framing.match(line.strip()))

	# ── how a session is brought to speak it ──

	def prepare(self, conn: ConfigSession) -> str | None:
		"""Right after login: the session made ready for commands - here,
		privileged (Netmiko's enable()).

		:returns: the problem when it can't be (nothing is sent then)
		:raises EnableFailed: enable() failed - caught right at the call, so
		 its ReadTimeout / ValueError are told apart from any other"""
		try:
			conn.enable()
		except (ValueError, ReadTimeout) as e:
			raise EnableFailed(str(e)) from e
		return None

	def read_config(self, conn: ConfigSession) -> str:
		""":returns: the configuration now, in the form the judge reads,
		 its framing left out (out of config mode)
		:raises: Netmiko's errors when it can't be read"""
		output = ""
		for command in self.show_config:
			# text: no parsing is asked for (TextFSM, TTP, Genie); whole, even
			# when a device message redrew the prompt mid-output (read_whole)
			output = read_whole(conn, command)
		return self.config(output)

	def read_saved(self, conn: ConfigSession) -> str:
		""":returns: the saved configuration (show_saved; the platform has one)"""
		return read_whole(conn, self.show_saved)

	def read_defaults(self, conn: ConfigSession, fetches: list[str]) -> str:
		""":returns: the configuration shown with its defaults, for the
		 commands the defaults fetch named (one view after the other)"""
		return "\n".join(self.config(read_whole(conn, fetch)) for fetch in fetches)

	def enter_config(self, conn: ConfigSession) -> str | None:
		"""Into configuration mode, where the commands go as typed.

		:returns: the problem when it was refused (nothing is sent then)"""
		conn.config_mode()
		return None

	def before_finish(self, conn: ConfigSession, report: Report, device: Target) -> None:
		"""After the last command, before the finish: what the platform needs
		done in configuration mode first (nothing here)."""

	def leave_config(self, conn: ConfigSession) -> None:
		"""Out of configuration mode (after the finish's before_leave)."""
		conn.exit_config_mode()

	def unsaved(self, conn: ConfigSession) -> tuple[str, str] | None:
		"""After the finish, where the platform tells its save state by a
		value: whether the change is saved.

		:returns: (the command asked, its answer) when it isn't; None when
		 it is, or the platform has no such state"""
		return None


@dataclass(frozen=True)
class PanOS(Platform):
	"""PAN-OS: the configuration as set lines is a configure-mode "show"
	once the session's output format is set - the documented way (the
	operational "show config running" isn't documented to honour it). In
	configure mode "show" prints the candidate: before the push, the
	running configuration plus anyone's pending edits; after our commit,
	the running one - pending edits are in both reads, never taken for
	ours. No enable step (Netmiko: NoEnable). Not yet run on a device."""

	def prepare(self, conn: ConfigSession) -> str | None:
		conn.send_command("set cli config-output-format set")
		return None

	def read_config(self, conn: ConfigSession) -> str:
		conn.config_mode()
		try:
			output = read_whole(conn, "show")
		finally:
			conn.exit_config_mode()
		return self.config(output)


@dataclass(frozen=True)
class IosXr(Platform):
	"""IOS-XR prints a time stamp above every show's output ("Thu Oct 21
	10:00:00.123 UTC"): switched off for the session, as Oxidized does - the
	framing pattern stays for a device that ignores it. No enable step
	(Netmiko: NoEnable)."""

	def prepare(self, conn: ConfigSession) -> str | None:
		conn.send_command("terminal exec prompt no-timestamp")
		return None


@dataclass(frozen=True)
class Comware(Platform):
	"""HP Comware has no enable step: Netmiko's enable() for it is
	"system-view", its configuration view. The session stays in user view
	until enter_config ("display" and "save" work in any view; Netmiko's
	save_config enters system-view itself)."""

	def prepare(self, conn: ConfigSession) -> str | None:
		return None


@dataclass(frozen=True)
class Gaia(Platform):
	"""Check Point Gaia (clish). An account whose login shell is expert
	(bash) would take every "set …" as bash's own set builtin - nothing
	configured, nothing refused: the session leaves it for clish, or the
	device is refused. Netmiko's enable() is that expert shell: never
	called. Saved or not is a value: "show config-state" → saved / unsaved."""
	wrong_shell: tuple[str, ...] = ("expert@", "#")   # the prompt marks of bash…
	cli_shell: str = "clish"                           # …and what leaves it
	saved_state: tuple[str, str] = ("show config-state", "saved")

	def prepare(self, conn: ConfigSession) -> str | None:
		if not self._in_bash(conn.find_prompt()):
			return None
		conn.send_command_timing(self.cli_shell)
		conn.set_base_prompt()
		if not self._in_bash(conn.find_prompt()):
			return None
		return (f"the account logs in to the wrong shell and '{self.cli_shell}' didn't "
		        f"leave it - set its shell to clish")

	def _in_bash(self, prompt: str) -> bool:
		prompt = prompt.strip().lower()
		return any(prompt.endswith(m) if m == "#" else m in prompt for m in self.wrong_shell)

	def unsaved(self, conn: ConfigSession) -> tuple[str, str] | None:
		ask, saved = self.saved_state
		# the last line answers: a notice may come first ("CLINFR0771 Config
		# lock is owned by admin")
		state = (str(conn.send_command(ask)).strip().splitlines() or [""])[-1].strip()
		return None if state.lower() == saved else (ask, state)


@dataclass(frozen=True)
class Junos(Platform):
	"""Junos: "configure private" - our commit can't take other users'
	pending edits along, and our rollback can't wipe them. Junos refuses it
	while someone has uncommitted shared edits: the device then fails, with
	the reason. A refused command's echo comes in pieces ("set snmp locatoin",
	then the line redrawn): echo_whole off."""
	config_command: str = "configure private"
	echo_whole: bool = False

	def enter_config(self, conn: ConfigSession) -> str | None:
		try:
			conn.config_mode(config_command=self.config_command)
		except (ValueError, ReadTimeout) as e:
			return (f"'{self.config_command}' was refused - usually another session has "
			        f"uncommitted changes in the shared configuration: commit or discard "
			        f"them (they're someone else's work), then rerun. Nothing was sent ({e})")
		return None


@dataclass(frozen=True)
class AosCx(Platform):
	"""Aruba AOS-CX: Netmiko recognises only its top-level "(config)#" prompt -
	from a section, "end" first, or leaving config mode sends nothing."""

	def leave_config(self, conn: ConfigSession) -> None:
		conn.send_command_timing("end")
		conn.exit_config_mode()


@dataclass(frozen=True)
class FortiOS(Platform):
	"""FortiOS: a config block applies at "end" - one left open is discarded
	(and Netmiko's own cleanup would run inside it): closed before the
	finish. Changes are saved automatically unless cfg-save is manual (lost
	at reboot) or revert (undone after a timeout) - then saved explicitly,
	under "config global" with VDOMs."""

	def before_finish(self, conn: ConfigSession, report: Report, device: Target) -> None:
		for _ in range(10):
			if "(" not in conn.find_prompt():
				break
			conn.send_command_timing("end")
		vdoms = getattr(conn, "_vdoms", False)
		if vdoms:
			conn.send_command_timing("config global")
		mode = conn.send_command_timing("get system global | grep cfg-save")
		if "manual" in mode or "revert" in mode:
			answer = one_line(conn.send_command_timing("execute cfg save"), "execute cfg save")
			report.notify(f"{device.endpoint}: cfg-save is not automatic - saved with "
			              f"'execute cfg save'" + (f" (the device replied: {answer})" if answer else ""),
			              Tone.WARNING)
		if vdoms:
			conn.send_command_timing("end")


# "ntp clock-period N" is written by classic IOS itself and drifts
_IOS_FRAMING = re.compile(r"Building configuration|Current configuration :|ntp clock-period ")
# "Thu Oct 21 10:00:00.123 UTC" above every show's output
_XR_FRAMING = re.compile(r"Building configuration|[A-Z][a-z]{2} [A-Z][a-z]{2} +\d+ \d\d:\d\d:\d\d(\.\d+)? \S+$")

# Every row declares its grammar in full: exec words, how it shows its
# defaults (no_defaults where there's no documented or verified way: those
# commands stay in review), the shapes of its event lines.
# Each platform's words that leave or close a section, and that remove.
# "quit" closes IOS's "crypto pki certificate chain"; IOS-XR closes
# route-policies, sets and groups at column 0; Junos / PAN-OS "edit" is
# followed by _typed; Gaia has no configuration mode
_CISCO_NAV = frozenset({"exit", "end", "quit"})
_XR_NAV = frozenset({"exit", "end", "root", "abort", "end-policy", "end-set", "end-group"})
_COMWARE_NAV = frozenset({"quit", "return"})
_SET_NAV = frozenset({"exit", "quit", "top", "up"})
_FORTIOS_NAV = frozenset({"next", "end", "abort"})
_NO, _UNDO, _DELETE = frozenset({"no"}), frozenset({"undo"}), frozenset({"delete"})

_CISCO_EXEC = ("do",)
# NX-OS, EOS and AOS-CX run exec commands in config mode without "do" (EOS
# and AOS-CX documented; AOS-CX: "with or without using the do command")
_CONFIG_EXEC = ("do", "show", "copy", "write", "ping", "traceroute", "clear")
_RUN_ALL = whole_config("show running-config all")

PLATFORMS: dict[str, Platform] = {
	# IOS, IOS-XE, NX-OS: "show running-config all" verified in the lab (IOS
	# 12.4, NX-OS 7.3); IOS-XE documented as IOS
	"cisco_ios": Platform(
		SaveConfig(), framing=_IOS_FRAMING, show_saved="show startup-config",
		exec_words=_CISCO_EXEC, defaults=_RUN_ALL, navigation=_CISCO_NAV, negation=_NO,
		about={"left_out": "Current configuration : N; ntp clock-period (written by IOS, drifts)",
		       "saved": "write mem; show startup-config",
		       "events": "*/. time stamps, uptime stamps, sequence numbers, %FAC-N-MNEMONIC"},
		sources={"read": "lab", "left_out": "doc", "saved": "doc", "layout": "lab", "navigation": "doc",
		         "removal": "lab", "exec": "lab", "defaults": "lab", "events": "doc"}),
	"cisco_xe": Platform(
		SaveConfig(), framing=_IOS_FRAMING, show_saved="show startup-config",
		exec_words=_CISCO_EXEC, defaults=_RUN_ALL, navigation=_CISCO_NAV, negation=_NO,
		about={"left_out": "as IOS", "saved": "write mem; show startup-config", "events": "as IOS"},
		sources={"read": "doc", "left_out": "doc", "saved": "doc", "layout": "doc", "navigation": "doc",
		         "removal": "doc", "exec": "doc", "defaults": "doc", "events": "doc"}),
	"cisco_nxos": Platform(
		SaveConfig(), show_saved="show startup-config",
		exec_words=_CONFIG_EXEC, defaults=_RUN_ALL, navigation=_CISCO_NAV, negation=_NO,
		about={"left_out": "nothing (its header lines are ! comments)",
		       "saved": "copy running-config startup-config; show startup-config",
		       "exec": "do, show (lab); copy, write, ping, traceroute, clear (inferred)",
		       "events": "2026 Oct 21 10:00:00 … %ETHPORT-5-IF_UP"},
		sources={"read": "lab", "left_out": "lab", "saved": "doc", "layout": "lab", "navigation": "doc",
		         "removal": "lab", "exec": "lab", "defaults": "lab", "events": "lab"}),
	# a failed commit reverts the running config; leaving config mode
	# (Netmiko answers "no" to "commit them?") drops the pending changes.
	# No documented defaults view
	"cisco_xr": IosXr(
		Commit(reverts_on_failure=True), framing=_XR_FRAMING,
		exec_words=("do", "show"), events=_XR_EVENTS, navigation=_XR_NAV, negation=_NO,
		about={"left_out": "the time stamp above every show (switched off: terminal exec prompt "
		                   "no-timestamp)",
		       "saved": "commit (atomic: a failure reverts); no startup configuration",
		       "defaults": "none (none documented)",
		       "events": "RP/0/RP0/CPU0:Oct 21 10:00:00.123 UTC: … (a year first: inferred)"},
		sources={"read": "doc", "left_out": "doc", "saved": "doc", "layout": "doc", "navigation": "doc",
		         "removal": "doc", "exec": "doc", "defaults": "unverified", "events": "doc"}),
	# "show running-config all": EOS (Ansible's eos_config defaults; Arista
	# also documents "show active all"), AOS-CX ("all: Shows all default
	# values for the current running configuration")
	"arista_eos": Platform(
		SaveConfig(), show_saved="show startup-config",
		exec_words=_CONFIG_EXEC, defaults=_RUN_ALL, navigation=_CISCO_NAV, negation=_NO,
		about={"left_out": "nothing (! comments)", "saved": "copy running-config startup-config",
		       "events": "Oct 21 10:00:00 sw1 Ebra: %LINEPROTO-5-UPDOWN; ISO stamps"},
		sources={"read": "doc", "left_out": "doc", "saved": "doc", "layout": "doc", "navigation": "doc",
		         "removal": "doc", "exec": "doc", "defaults": "doc", "events": "doc"}),
	"aruba_aoscx": AosCx(
		SaveConfig(), show_saved="show startup-config",
		exec_words=_CONFIG_EXEC, defaults=_RUN_ALL, navigation=_CISCO_NAV, negation=_NO,
		about={"left_out": "nothing (Current configuration: never changes)",
		       "saved": "copy running-config startup-config ('end' first to leave config mode)",
		       "events": "ISO stamps"},
		sources={"read": "doc", "left_out": "doc", "saved": "doc", "layout": "doc", "navigation": "doc",
		         "removal": "doc", "exec": "doc", "defaults": "doc", "events": "inferred"}),
	# ProCurve's config levels have the Operator and Manager commands; no
	# defaults view (every view shows only what differs from the default)
	"hp_procurve": Platform(
		SaveConfig(), show_saved="show config",
		exec_words=("show", "write", "ping", "copy", "traceroute", "boot", "reload"),
		events=_PROCURVE_EVENTS, navigation=_CISCO_NAV, negation=_NO,
		about={"left_out": "nothing (its header changes only with firmware)",
		       "saved": "write memory; show config", "layout": "indented sections, closed by exit",
		       "defaults": "none (every view shows only non-defaults)",
		       "events": "I 10/21/26 10:00:00 … (the event log's shape)"},
		sources={"read": "doc", "left_out": "inferred", "saved": "doc", "layout": "doc", "navigation": "doc",
		         "removal": "doc", "exec": "doc", "defaults": "doc", "events": "doc"}),
	# Comware: display / save in any view (ping / tracert too on V7);
	# "display current-configuration" has no "all" in V7 - not relied on
	"hp_comware": Comware(
		SaveConfig(), ("display current-configuration",), show_saved="display saved-configuration",
		exec_words=("display", "save", "ping", "tracert"), events=_COMWARE_EVENTS,
		navigation=_COMWARE_NAV, negation=_UNDO,
		about={"left_out": "nothing", "saved": "save force; display saved-configuration",
		       "layout": "views, # separators", "exec": "display, save (doc); ping, tracert (inferred)",
		       "defaults": "none (no 'all' in V7)", "events": "%Oct 21 … / #… traps / %0.1… before the clock"},
		sources={"read": "doc", "left_out": "doc", "saved": "doc", "layout": "doc", "navigation": "doc",
		         "removal": "doc", "exec": "doc", "defaults": "doc", "events": "doc"}),
	# configuration mode's own non-configuration commands: run, show, status,
	# help, save (to a file); defaults aren't shown as set lines
	"juniper_junos": Junos(
		Commit(discard=("rollback 0",), pending="show | compare"), ("show configuration | display set",),
		flat=True, exec_words=("run", "show", "status", "help", "save"),
		navigation=_SET_NAV, negation=_DELETE,
		about={"left_out": "nothing (## comments)",
		       "saved": "commit in configure private; discard rollback 0, check show | compare",
		       "navigation": "edit, exit, quit, top, up (the path unknown after up)",
		       "events": "Message from syslogd@…"},
		# lab: JS1, vJunos-switch 24.4R1.9 (2026-10-24)
		sources={"read": "lab", "left_out": "lab", "saved": "lab", "layout": "lab", "navigation": "lab",
		         "removal": "lab", "exec": "lab", "defaults": "doc", "events": "inferred"}),
	# revert config (8.0+); loading the running config into the candidate
	# discards too, also on older releases. Both act on the shared candidate
	"paloalto_panos": PanOS(
		Commit(discard=("revert config", "load config from running-config.xml"),
		       pending="run show config diff", by_pending=True),
		("show",), flat=True, framing=re.compile(r"\[edit"),
		exec_words=("run", "show", "check", "validate", "find", "save"),
		navigation=_SET_NAV, negation=_DELETE,
		about={"read": "set cli config-output-format set, then configure / show (the candidate)",
		       "left_out": "the [edit] banner",
		       "saved": "commit, decided by run show config diff after it (nothing pending: not committed); "
		                "discard revert config (the shared candidate)",
		       "navigation": "edit, exit, quit, top, up (the path unknown after up)",
		       "events": "none known"},
		# lab: PA1, PAN-OS 11.2.5 (2026-10-24)
		sources={"read": "lab", "left_out": "lab", "saved": "lab", "layout": "lab", "navigation": "lab",
		         "removal": "lab", "exec": "lab", "defaults": "doc", "events": "unverified"}),
	# "lock database override" opens most snippets
	"checkpoint_gaia": Gaia(
		RunCommand("save config"), ("show configuration",), flat=True,
		exec_words=("show", "lock", "unlock", "save"), navigation=frozenset(), negation=_DELETE,
		about={"left_out": "nothing (# header)",
		       "saved": "save config; show config-state's last line = saved; never enable() (the expert shell)",
		       "layout": "flat set / add lines", "events": "Broadcast message from …"},
		sources={"read": "doc", "left_out": "doc", "saved": "doc", "layout": "doc", "navigation": "doc",
		         "removal": "doc", "exec": "doc", "defaults": "doc", "events": "inferred"}),
	# Netmiko sets the console output to "standard" for the session (when it
	# was "more") and puts it back at logout - the same in both reads
	"fortinet": FortiOS(
		NoFinish(), ("show",), close_blocks=True,
		exec_words=("get", "show", "execute", "diagnose", "tree", "sudo", "fnsysctl"),
		defaults=fortios_blocks, navigation=_FORTIOS_NAV, negation=frozenset({"unset", "delete"}),
		about={"left_out": "nothing (#config-version comments)",
		       "saved": "live; execute cfg save when cfg-save is manual / revert",
		       "defaults": "show full-configuration <section> (not under vdom / global)",
		       "events": "none seen"},
		sources={"read": "lab", "left_out": "lab", "saved": "doc", "layout": "lab", "navigation": "doc",
		         "removal": "lab", "exec": "lab", "defaults": "lab", "events": "lab"}),
}

# PAN-OS commits can take minutes; Netmiko waits 120 s by default
COMMIT_TIMEOUT = 300
# Printing a large firewall config (PAN-OS, FortiOS) takes a while
FETCH_TIMEOUT = 120
# After Netmiko's read returns: how long the device must stay quiet for the
# read to count as whole, and at most how long the rest may take. A message
# the device prints (syslog, a broadcast) makes it redraw its prompt, and
# Netmiko's read ends at that prompt - in the middle of the output
QUIET = 1.0
REST_LIMIT = 60.0


def late_output(conn: ConfigSession, quiet: float = QUIET) -> str:
	"""What the device printed after Netmiko's read returned: read until it
	has been quiet for `quiet` seconds (0: only what is already there), at
	most REST_LIMIT.

	:returns: the text, "" when nothing came"""
	chunks: list[str] = []
	started = last = time.monotonic()
	while time.monotonic() - started < REST_LIMIT:
		chunk = conn.read_channel()
		if not isinstance(chunk, str):      # a channel gives text; anything else: nothing
			break
		if chunk:
			chunks.append(chunk)
			last = time.monotonic()
		elif time.monotonic() - last >= quiet:
			break
		else:
			time.sleep(0.1)
	return "".join(chunks)


def read_whole(conn: ConfigSession, command: str) -> str:
	"""A command's whole output: Netmiko's read, plus what still came after
	it - a prompt redrawn after a device message ends Netmiko's read in the
	middle; the redrawn prompts are left out.

	:returns: the output (an empty one is the caller's to refuse)"""
	output = str(conn.send_command(command, read_timeout=FETCH_TIMEOUT))
	rest = late_output(conn)
	if rest.strip():
		# a redrawn prompt, alone on its line or with the output going on after it
		prompt = re.compile(re.escape(str(getattr(conn, "base_prompt", "")).strip()) + r"\s*[#>$%]?\s*")
		lines = [prompt.sub("", line, count=1) if prompt.match(line.strip()) else line
		         for line in (raw.strip("\r") for raw in rest.splitlines())]
		output = output.rstrip("\n") + "\n" + "\n".join(line for line in lines if line.strip())
	return output
