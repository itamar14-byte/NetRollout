"""What NetRollout knows about each platform (Netmiko device type), with no
I/O of its own: its grammar - how a push finishes (the Finish family: save /
commit / a command / nothing - acting on the session it is handed), how its
config prints and is read back (PLATFORMS), what frames that output. Never
what its messages mean: a push is judged by the device's own configuration
(src/rollout/evidence.py). The SSH conversation is src/rollout/session.py's.
Adding a vendor: one PLATFORMS row plus a fixture in
tests/unit/rollout/test_platforms.py."""
from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from netmiko.exceptions import ReadTimeout

from src.rollout.log import Tone


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


class Commit(Finish):
	"""commit() before leaving config mode (leaving discards uncommitted
	changes).

	:param discard: after a failed commit, the commands that drop the
	 candidate - all sent in order (whether one was accepted isn't judged
	 from its wording: the device's replies are shown, a person confirms)
	:param reverts_on_failure: the platform drops a failed commit by itself
	 (IOS XR): no discard needed, nothing left to check"""

	def __init__(self, discard: tuple[str, ...] = (),
	             reverts_on_failure: bool = False) -> None:
		self.discard = discard
		self.reverts_on_failure = reverts_on_failure

	@property
	def interruptible(self) -> bool:
		return False

	def before_leave(self, conn: ConfigSession, report: Report,
	                 device: Target) -> bool:
		try:
			conn.commit(read_timeout=COMMIT_TIMEOUT)
		except ReadTimeout:
			# It may still complete on the device: don't claim either way
			report.action_needed(device, f"the commit didn't finish within "
			                     f"{COMMIT_TIMEOUT}s - check on the device "
			                     f"whether it went through (reported as failed)")
			return False
		except ValueError as e:
			report.notify(f"{device.endpoint}: commit failed - nothing was "
			              f"applied. {e}", Tone.ERROR)
			if self.discard:
				prompt = str(getattr(conn, "base_prompt", "")).strip()
				replies = []
				for command in self.discard:
					reply = conn.send_config_set([command], exit_config_mode=False)
					said = " ".join(line.strip() for line in str(reply).splitlines()
					                if line.strip() and command not in line
					                and not (prompt and line.strip().startswith(prompt)))
					replies.append(f"'{command}'" + (f" (the device replied: {said})" if said else ""))
				report.action_needed(device, "the commit failed and the failed changes "
				                     f"were discarded with {', then '.join(replies)} - "
				                     "check that the candidate configuration is empty")
			elif not self.reverts_on_failure:
				report.action_needed(device, "the failed changes may still be "
				                     "in the candidate configuration - discard "
				                     "them on the device")
			conn.exit_config_mode()
			return False
		return True

	def after_leave(self, conn: ConfigSession) -> tuple[Any, str]:
		return "", ""


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
	"juniper_junos": Platform(Commit(discard=("rollback 0",)),
	                          ("show configuration | display set",), flat=True,
	                          config_command="configure private"),
	# revert config (8.0+); loading the running config into the candidate
	# discards too, also on older releases
	"paloalto_panos": Platform(Commit(discard=(
		"revert config", "load config from running-config.xml")),
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
