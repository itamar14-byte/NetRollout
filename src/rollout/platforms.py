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
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from netmiko.exceptions import ReadTimeout

from src.rollout.log import Tone


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
		"""Finish from a fresh session (privileged, out of config mode).

		:param connect: opens it
		:returns: as after_leave; None when there is nothing to finish (no
		 session opened)"""
		with connect() as conn:
			conn.enable()
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
	# what show_config prints above the config itself (IOS: "Building
	# configuration...", "Current configuration : 1290 bytes" - a count that
	# changes with every push): lines starting with these are left out
	framing: tuple[str, ...] = ()
	# the saved configuration, to check a save kept the change; "" when the
	# platform has none to show (a commit is the save; FortiOS saves itself)
	show_saved: str = ""
	# a command whose answer is the save state, and the value meaning saved
	# (Gaia: "show config-state" → "saved" / "unsaved") - a value read, as
	# FortiOS's cfg-save mode is, never a message interpreted
	saved_state: tuple[str, str] | None = None

	def config(self, output: str) -> str:
		""":returns: show_config's output without its framing lines"""
		if not self.framing:
			return output
		return "\n".join(line for line in output.splitlines()
		                 if not line.strip().startswith(self.framing))


_IOS_FRAMING = ("Building configuration", "Current configuration :")

PLATFORMS = {
	"cisco_ios": Platform(SaveConfig(), framing=_IOS_FRAMING, show_saved="show startup-config"),
	"cisco_xe": Platform(SaveConfig(), framing=_IOS_FRAMING, show_saved="show startup-config"),
	"cisco_nxos": Platform(SaveConfig(), show_saved="show startup-config"),
	# a failed commit reverts the running config; leaving config mode
	# (Netmiko answers "no" to "commit them?") drops the pending changes
	"cisco_xr": Platform(Commit(reverts_on_failure=True)),
	"arista_eos": Platform(SaveConfig(), show_saved="show startup-config"),
	"aruba_aoscx": Platform(SaveConfig(), leave_first="end", show_saved="show startup-config"),
	"hp_procurve": Platform(SaveConfig(), show_saved="show config"),
	"hp_comware": Platform(SaveConfig(), ("display current-configuration",),
	                       show_saved="display saved-configuration"),
	# private: our commit can't take other users' pending edits along, and
	# our rollback can't wipe them (Junos refuses it while someone has
	# uncommitted shared edits - the device then fails, with the reason)
	"juniper_junos": Platform(Commit(discard=("rollback 0",), pending="show | compare"),
	                          ("show configuration | display set",), flat=True,
	                          config_command="configure private"),
	# revert config (8.0+); loading the running config into the candidate
	# discards too, also on older releases
	"paloalto_panos": Platform(Commit(discard=(
		"revert config", "load config from running-config.xml"),
		pending="run show config diff"),
		("set cli config-output-format set", "show config running"), flat=True),
	# An account whose shell is expert (bash) would take every "set …" as
	# bash's own set builtin (nothing configured, nothing refused): switch to
	# clish first, or refuse
	"checkpoint_gaia": Platform(RunCommand("save config"),
	                            ("show configuration",), flat=True,
	                            wrong_shell=("expert@", "#"), cli_shell="clish",
	                            saved_state=("show config-state", "saved")),
	"fortinet": Platform(NoFinish(), ("show",), close_blocks=True),
}

# PAN-OS commits can take minutes; Netmiko waits 120 s by default
COMMIT_TIMEOUT = 300
# Printing a large firewall config (PAN-OS, FortiOS) takes a while
FETCH_TIMEOUT = 120
