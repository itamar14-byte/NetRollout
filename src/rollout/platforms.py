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
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from netmiko.exceptions import ReadTimeout

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
		if not text or set(text) <= {"^"} or _CONTEXT.match(text):
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
	                    enter_config_mode: bool = True) -> str: ...

	def send_command(self, command_string: str, *,
	                 read_timeout: float = 10.0) -> Any: ...

	def send_command_timing(self, command_string: str) -> Any: ...

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
	 read, not a message interpreted; "" - a person checks
	:param reverts_on_failure: the platform drops a failed commit by itself
	 (IOS XR): no discard needed, nothing left to check"""

	def __init__(self, discard: tuple[str, ...] = (), pending: str = "",
	             reverts_on_failure: bool = False) -> None:
		self.discard = discard
		self.pending = pending
		self.reverts_on_failure = reverts_on_failure

	@property
	def interruptible(self) -> bool:
		return False

	def before_leave(self, conn: ConfigSession, report: Report,
	                 device: Target) -> bool:
		try:
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


@dataclass(frozen=True)
class Platform:
	"""How a platform finishes a push and prints its config in the syntax
	engineers type - its grammar, never what its messages mean."""
	finish: Finish
	show_config: tuple[str, ...] = ("show running-config",)
	flat: bool = False         # one "set …" line per setting, no sections
	close_blocks: bool = False  # FortiOS: an open config block is discarded
	config_command: str = ""   # instead of the driver's default config mode
	leave_first: str = ""      # sent before leaving config mode (Aruba CX:
	                           # Netmiko only recognises "(config)#")
	wrong_shell: tuple[str, ...] = ()  # prompt marks of the wrong shell…
	cli_shell: str = ""                # …and the command that leaves it
	# lines show_config prints that aren't the configuration and change by
	# themselves (IOS "Current configuration : 1290 bytes", IOS-XR's time
	# stamp above every show's output): a line matching it is left out
	framing: re.Pattern[str] | None = None
	# the saved configuration, to check a save kept the change; "" when the
	# platform has none to show (a commit is the save; FortiOS saves itself)
	show_saved: str = ""
	# a command whose answer is the save state, and the value meaning saved
	# (Gaia: "show config-state" → "saved" / "unsaved") - a value read, as
	# FortiOS's cfg-save mode is, never a message interpreted
	saved_state: tuple[str, str] | None = None
	# first words that run an exec command from config mode (IOS "do show
	# …", FortiOS "get …"): not configuration - shown, never judged
	exec_words: tuple[str, ...] = ()
	# how the configuration prints with its defaults (DefaultsFetch)
	defaults: DefaultsFetch = no_defaults
	# whether the CLI has an enable step (Netmiko's enable()); Gaia's is its
	# expert shell - bash, where every "set" is bash's own: never entered
	enables: bool = True
	# the shapes of a logged event line (EVENTS and the platform's own)
	events: re.Pattern[str] = EVENTS

	def config(self, output: str) -> str:
		""":returns: show_config's output without its framing lines"""
		if self.framing is None:
			return output
		framing = self.framing
		return "\n".join(line for line in output.splitlines()
		                 if not framing.match(line.strip()))


# "ntp clock-period N" is written by classic IOS itself and drifts
_IOS_FRAMING = re.compile(r"Building configuration|Current configuration :|ntp clock-period ")
# "Thu Oct 21 10:00:00.123 UTC" above every show's output
_XR_FRAMING = re.compile(r"Building configuration|[A-Z][a-z]{2} [A-Z][a-z]{2} +\d+ \d\d:\d\d:\d\d(\.\d+)? \S+$")

# Every row declares its grammar in full: exec words, how it shows its
# defaults (no_defaults where there's no documented or verified way: those
# commands stay in review), the shapes of its event lines.
_CISCO_EXEC = ("do",)
# NX-OS, EOS and AOS-CX run exec commands in config mode without "do" (EOS
# and AOS-CX documented; AOS-CX: "with or without using the do command")
_CONFIG_EXEC = ("do", "show", "copy", "write", "ping", "traceroute", "clear")
_RUN_ALL = whole_config("show running-config all")

PLATFORMS = {
	# IOS, IOS-XE, NX-OS: "show running-config all" verified in the lab (IOS
	# 12.4, NX-OS 7.3); IOS-XE documented as IOS
	"cisco_ios": Platform(SaveConfig(), framing=_IOS_FRAMING, show_saved="show startup-config",
	                      exec_words=_CISCO_EXEC, defaults=_RUN_ALL),
	"cisco_xe": Platform(SaveConfig(), framing=_IOS_FRAMING, show_saved="show startup-config",
	                     exec_words=_CISCO_EXEC, defaults=_RUN_ALL),
	"cisco_nxos": Platform(SaveConfig(), show_saved="show startup-config",
	                       exec_words=_CONFIG_EXEC, defaults=_RUN_ALL),
	# a failed commit reverts the running config; leaving config mode
	# (Netmiko answers "no" to "commit them?") drops the pending changes.
	# No documented defaults view
	"cisco_xr": Platform(Commit(reverts_on_failure=True), framing=_XR_FRAMING,
	                     exec_words=("do", "show"), events=_XR_EVENTS),
	# "show running-config all": EOS (Ansible's eos_config defaults; Arista
	# also documents "show active all"), AOS-CX ("all: Shows all default
	# values for the current running configuration")
	"arista_eos": Platform(SaveConfig(), show_saved="show startup-config",
	                       exec_words=_CONFIG_EXEC, defaults=_RUN_ALL),
	"aruba_aoscx": Platform(SaveConfig(), leave_first="end", show_saved="show startup-config",
	                        exec_words=_CONFIG_EXEC, defaults=_RUN_ALL),
	# ProCurve's config levels have the Operator and Manager commands; no
	# defaults view (every view shows only what differs from the default)
	"hp_procurve": Platform(SaveConfig(), show_saved="show config",
	                        exec_words=("show", "write", "ping", "copy", "traceroute", "boot", "reload"),
	                        events=_PROCURVE_EVENTS),
	# Comware: display / save in any view (ping / tracert too on V7);
	# "display current-configuration" has no "all" in V7 - not relied on
	"hp_comware": Platform(SaveConfig(), ("display current-configuration",),
	                       show_saved="display saved-configuration",
	                       exec_words=("display", "save", "ping", "tracert"), events=_COMWARE_EVENTS),
	# private: our commit can't take other users' pending edits along, and
	# our rollback can't wipe them (Junos refuses it while someone has
	# uncommitted shared edits - the device then fails, with the reason).
	# configuration mode's own non-configuration commands: run, show, status,
	# help, save (to a file); defaults aren't shown as set lines
	"juniper_junos": Platform(Commit(discard=("rollback 0",), pending="show | compare"),
	                          ("show configuration | display set",), flat=True,
	                          config_command="configure private",
	                          exec_words=("run", "show", "status", "help", "save")),
	# revert config (8.0+); loading the running config into the candidate
	# discards too, also on older releases
	"paloalto_panos": Platform(Commit(discard=(
		"revert config", "load config from running-config.xml"),
		pending="run show config diff"),
		("set cli config-output-format set", "show config running"), flat=True,
		exec_words=("run", "show", "check", "validate", "find", "save")),
	# An account whose shell is expert (bash) would take every "set …" as
	# bash's own set builtin (nothing configured, nothing refused): switch to
	# clish first, or refuse - and never enable() (its expert shell). "lock
	# database override" opens most snippets
	"checkpoint_gaia": Platform(RunCommand("save config"),
	                            ("show configuration",), flat=True,
	                            wrong_shell=("expert@", "#"), cli_shell="clish",
	                            saved_state=("show config-state", "saved"),
	                            exec_words=("show", "lock", "unlock", "save"), enables=False),
	"fortinet": Platform(NoFinish(), ("show",), close_blocks=True,
	                     exec_words=("get", "show", "execute", "diagnose", "tree", "sudo", "fnsysctl"),
	                     defaults=fortios_blocks),
}

# PAN-OS commits can take minutes; Netmiko waits 120 s by default
COMMIT_TIMEOUT = 300
# Printing a large firewall config (PAN-OS, FortiOS) takes a while
FETCH_TIMEOUT = 120
