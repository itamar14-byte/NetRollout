"""What NetRollout knows about each platform (Netmiko device type), with no
I/O: how a push finishes (save / commit / a command / nothing) and how the
config prints (PLATFORMS), what a refused command looks like (rejection),
and how a typed command is found in — or confirmed gone from — a fetched
config (verify_commands). The engine (src/rollout/engine.py) does the SSH; the web
pages use this directly. Adding a vendor: one PLATFORMS row plus a fixture
in tests/unit/test_platforms.py."""
import re
from dataclasses import dataclass


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
	# after a failed commit: drop the candidate — tried in order until one
	# is accepted
	discard: tuple[str, ...] = ()
	close_blocks: bool = False  # FortiOS: an open config block is discarded
	config_command: str = ""   # instead of the driver's default config mode
	leave_first: str = ""      # sent before leaving config mode (Aruba CX:
	                           # Netmiko only recognises "(config)#")
	wrong_shell: tuple[str, ...] = ()  # prompt marks of the wrong shell…
	cli_shell: str = ""                # …and the command that leaves it


PLATFORMS = {
	"cisco_ios": Platform("save"),
	"cisco_xe": Platform("save"),
	"cisco_nxos": Platform("save"),
	# a failed commit reverts the running config; leaving config mode
	# (Netmiko answers "no" to "commit them?") drops the pending changes
	"cisco_xr": Platform("commit"),
	"arista_eos": Platform("save"),
	"aruba_aoscx": Platform("save", leave_first="end"),
	"hp_procurve": Platform("save"),
	"hp_comware": Platform("save", ("display current-configuration",)),
	# private: our commit can't take other users' pending edits along, and
	# our rollback can't wipe them (Junos refuses it while someone has
	# uncommitted shared edits — the device then fails, with the reason)
	"juniper_junos": Platform("commit", ("show configuration | display set",),
	                          flat=True, discard=("rollback 0",),
	                          config_command="configure private"),
	# revert config (8.0+); loading the running config into the candidate
	# discards too, also on older releases
	"paloalto_panos": Platform("commit", ("set cli config-output-format set",
	                                      "show config running"), flat=True,
	                           discard=("revert config",
	                                    "load config from running-config.xml")),
	# An account whose shell is expert (bash) would take every "set …" as
	# bash's own set builtin (nothing configured, nothing refused): switch to
	# clish first, or refuse
	"checkpoint_gaia": Platform("save config", ("show configuration",),
	                            flat=True, wrong_shell=("expert@", "#"),
	                            cli_shell="clish"),
	"fortinet": Platform("", ("show",), close_blocks=True),
}

# PAN-OS commits can take minutes; Netmiko waits 120 s by default
COMMIT_TIMEOUT = 300
# Printing a large firewall config (PAN-OS, FortiOS) takes a while
FETCH_TIMEOUT = 120

# A device's reply to a command it refused (any vendor; matched with the
# command's own echo removed, so "description unknown-host" isn't one)
REJECTION_MARKERS = (
	"% invalid", "invalid input", "invalid command", "invalid syntax",
	"unknown command", "unrecognized command", "syntax error", "error:",
	"% error", "% incomplete", "incomplete command", "% ambiguous",
	"ambiguous command",
	"not in range", "missing argument",                       # Junos
	"failed to commit",                                       # IOS-XR
	"validation error", "commit failed",                      # PAN-OS
	"command fail", "parse error", "node_check_object fail",  # FortiOS
	"entry not found in datasource", "unmatched double quote",
	"invalid object",
	"% wrong parameter", "too many parameters",               # Comware
	"incomplete input", "ambiguous input",                    # ProCurve
	"% command incomplete",                                   # Aruba CX
	"command not found")                                      # Gaia expert
# Check Point Gaia prefixes its errors with a code: "CLINFR0349  Incomplete
# command.", "RTGRTG0019  OSPF: …" (at the start of a line, so a hostname in
# the prompt can't match)
_CODED_ERROR = re.compile(r"^[A-Z]{6}\d{4}\s")
# Leave or close a config section — they configure nothing
NAVIGATION = {"exit", "end", "next", "abort", "quit", "return", "top", "up",
              "root",                   # IOS-XR: back to the top of config
              "end-policy", "end-set"}  # IOS-XR: close route-policy / sets


def navigates(word: str) -> bool:
	""":returns: whether a command's first word only moves between config
	 modes (exit, end, quit, … - also IOS exit-address-family, exit-vrf)"""
	return word in NAVIGATION or word.startswith("exit-")

# Operational commands: they leave no trace in the config to check
NOT_VERIFIABLE = ("commit", "write", "copy ", "clear ", "do ", "show ", "save",
                  "reload", "ping ", "traceroute ", "request ", "run ")

# Per-command verify verdicts
VERIFIED, NOT_CONFIGURED, STILL_CONFIGURED = "verified", "not configured", \
	"still configured"
UNVERIFIABLE, VARIABLE = "not verifiable", "variable"


def rejection(output: str, command: str) -> str | None:
	"""The device's complaint about `command`, if any. The echo of the
	command itself is skipped, so its own words (e.g. "description
	invalid-vlan") are never mistaken for a complaint.

	:param output: what the device printed after the command
	:returns: the complaining line; None when the command was accepted"""
	for line in output.splitlines():
		text = line.strip()
		# On the echo line only the prompt before the command is the device's
		# ("Invalid input: foo" for the command "foo" is still a complaint)
		said = text[:-len(command)] if command and text.endswith(command) \
			else text
		if any(marker in said.lower() for marker in REJECTION_MARKERS) or \
				_CODED_ERROR.match(said):
			return text
	return None


def normalize(line: str) -> str:
	""":returns: a config line or command as compared - spacing, case and
	 FortiOS / ProCurve quoting don't change the meaning"""
	return " ".join(line.replace('"', "").split()).lower()


def first_word(command: str) -> str:
	""":returns: a command's first word as compared ("" when normalizing
	 leaves nothing - a blank line, or one of only quotes)"""
	return (normalize(command).split() or [""])[0]


class _Section:
	"""A config section: its lines (normalized), each with its own children."""
	__slots__ = ("children",)

	def __init__(self) -> None:
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
		node = stack[-1][1].children.setdefault(normalize(stripped), _Section())
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
		key = normalize(command)
		word = first_word(command)
		if navigates(word):
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
	lines = {normalize(line) for line in config.splitlines() if line.strip()}
	prefix: list[list[str]] = []
	verdicts = []
	for command in commands:
		words = normalize(command).split()
		word = words[0] if words else ""
		if word == "edit":
			prefix.append(words[1:])
			verdicts.append(UNVERIFIABLE)
		elif word in ("top", "end"):
			prefix = []
			verdicts.append(UNVERIFIABLE)
		elif navigates(word):
			prefix = prefix[:-1]
			verdicts.append(UNVERIFIABLE)
		elif " ".join(words).startswith(NOT_VERIFIABLE):
			verdicts.append(UNVERIFIABLE)
		else:
			path = [w for level in prefix for w in level]
			if word in ("set", "delete"):
				words = [word, *path, *words[1:]]
			if words[:2] == ["set", "static-route"] and words[-1] == "off" \
					and len(words) == 4:
				# Gaia: "set static-route <dst> off" removes the route
				words = ["delete", "static-route", words[2]]
				word = "delete"
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
	"""Check each pushed command against the config read back from the device.

	:param device_type: the Netmiko device type (how its config is laid out)
	:param config: the running config, as the platform prints it (PLATFORMS)
	:param commands: what was pushed, as typed
	:returns: one verdict per command, in order: verified / not configured /
	 still configured (a removal that didn't take) / not verifiable
	 (navigation, operational) / variable (an unresolved $$TOKEN$$ - the
	 rollout log has the real one)"""
	platform = PLATFORMS[device_type]
	verdicts = _verify_flat(config, commands) if platform.flat else \
		_verify_sectioned(config, commands, blocks=platform.close_blocks)
	return [VARIABLE if "$$" in command else verdict
	        for command, verdict in zip(commands, verdicts)]
