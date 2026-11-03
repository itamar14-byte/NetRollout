"""Which platform a device runs, asked of the device itself: signed in with
a profile (Netmiko's generic driver, a rollout's login), a few read-only
probes, each answer read until the prompt is back, and every platform's
Identity (src/rollout/platforms.py) checked against it - exactly one must
claim it. Never a guess: an answer no identity claims, or two do, is "don't
know". Used by Add Device's Detect button, and by Discover per address.

Not Netmiko's SSHDetect: it waits at least 7 s per command, stops early only
on a certain match (classic IOS never is one: all 16 of its probes, ~110 s),
checks other vendors' loose words (a hostname "nokia-edge" makes a Cisco a
Nokia) and never answers a pager. Its patterns are where ours started."""
from __future__ import annotations   # type hints are never evaluated

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from src.rollout.platforms import NAME_CISCO, PLATFORMS, Identity, hostname, text
from src.rollout.session import connect_unknown
if TYPE_CHECKING:   # annotations only: engine.py imports the session
	from src.rollout.engine import Device


# Platforms NetRollout doesn't support, named when found (their answers to
# our probes). ACI leaves and MDS print NX-OS's header (cisco_nxos rules them
# out); an FTD's answer can carry the ASA block. A bash shell answers "show
# version" with "command not found" (Junos's csh: "Command not found." - its
# root shell is Junos's own Shell)
_NXOS_HEADER = text("Cisco Nexus Operating System (NX-OS) Software")
NEIGHBOURS: dict[str, Identity] = {
	"cisco_aci": Identity("show version", _NXOS_HEADER, also=text("aci-n9000")),
	"cisco_mds": Identity("show version", _NXOS_HEADER, also=text("cisco MDS")),
	"cisco_ftd": Identity("show version", text("Cisco Firepower"), also=text("Threat Defense")),
	"cisco_asa": Identity("show version", text("Cisco Adaptive Security Appliance Software"),
	                      unless=text("Threat Defense")),
	"huawei": Identity("display version", text("Huawei Versatile Routing Platform")),
	"linux": Identity("show version", text("command not found")),
}
# …and what to call them ("Detected Cisco ASA, which NetRollout doesn't support")
NEIGHBOUR_NAMES = {"cisco_aci": "Cisco ACI", "cisco_mds": "Cisco MDS", "cisco_asa": "Cisco ASA",
                   "cisco_ftd": "Cisco Firepower Threat Defense", "huawei": "Huawei VRP",
                   "linux": "a Linux shell"}

# A pager waiting at the end of an answer, and the key that leaves it -
# ProCurve and AOS-CX name theirs ("quit: Control-C", "quit: q"). The
# identifying line is always on the first screen: the rest isn't needed
PAGERS: tuple[tuple[re.Pattern[str], str], ...] = (
	(re.compile(r"-- MORE --, next page: Space.*quit: Control-C\s*$"), "\x03"),
	(re.compile(r"(--\s*More\s*--|---\(more[^)]*\)---|-+ More -+|-- MORE --.*|lines \d+-\d+)\s*$",
	            re.IGNORECASE), "q"),
)
# ProCurve's splash after login (its Netmiko driver answers it too)
ANY_KEY = re.compile(r"press any key to continue", re.IGNORECASE)
# A login disclaimer to accept (FortiOS "(Press 'a' to accept):") - never
# accepted on the person's behalf
DISCLAIMER = re.compile(r"press\s+'?a'?\s+to\s+accept", re.IGNORECASE)
# Terminal escape codes some devices draw with (ProCurve, AOS-CX), carriage
# returns (NX-OS ends a line with two) and backspaces (a pager's erase)
_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|[\r\x08]")

PROBE_SECONDS = 15.0     # one probe's answer, at most
DETECT_SECONDS = 45.0    # every probe together, at most
_POLL = 0.1


@dataclass(frozen=True)
class Detection:
	"""What a device turned out to be."""
	platform: str | None = None   # one of PLATFORMS; None: not one NetRollout supports, or unknown
	detected: str | None = None   # what it is: a platform's or a neighbour's name; None: unknown
	hostname: str | None = None   # its name in its prompt; None: unreadable
	evidence: str | None = None   # the answer's line it was recognised by
	problem: str | None = None    # why nothing was detected, when there's more to say


class Channel(Protocol):
	"""The Netmiko connection methods detection uses (BaseConnection's)."""
	RETURN: str

	def find_prompt(self) -> str: ...

	def write_channel(self, out_data: str) -> None: ...

	def read_channel(self) -> str: ...

	def disconnect(self) -> None: ...


def detect(device: Device) -> Detection:
	"""Sign in to the device and tell its platform (identify). Its
	device_type isn't used; its credentials are already decrypted.

	:raises: Netmiko's exceptions when it can't sign in - authentication
	 failed, timed out - each its own (Discover counts the first)"""
	conn = connect_unknown(device)
	try:
		return identify(conn)
	finally:
		conn.disconnect()


def identities() -> dict[str, Identity]:
	""":returns: every identity, by name: the platforms', then the neighbours'"""
	ours = {name: p.identity for name, p in PLATFORMS.items() if p.identity is not None}
	return {**ours, **NEIGHBOURS}


def identify(conn: Channel, clock: Callable[[], float] = time.monotonic,
             pause: Callable[[float], None] = time.sleep) -> Detection:
	"""Probe a signed-in session until one identity claims an answer.

	:param clock: the time limits' clock; pause: the wait between reads (a
	 test's own)"""
	try:
		prompt = conn.find_prompt()
	except ValueError:
		return Detection(problem="The device showed no prompt")
	if ANY_KEY.search(prompt):
		conn.write_channel(conn.RETURN)
		prompt = conn.find_prompt()
	if DISCLAIMER.search(prompt):
		return Detection(problem="The device shows a login disclaimer")
	known = identities()
	deadline = clock() + DETECT_SECONDS
	for command, probe in probes(prompt, known):
		if clock() >= deadline:
			break
		answer = ask(conn, command, prompt, min(deadline, clock() + PROBE_SECONDS), clock, pause)
		claims = {name: line for name, identity in known.items()
		          if identity.probe == probe and (line := identity.evidence(answer))}
		if len(claims) > 1:
			return Detection(problem=f"Its answer fits {' and '.join(sorted(claims))}")
		if claims:
			name, line = claims.popitem()
			return Detection(platform=name if name in PLATFORMS else None, detected=name, evidence=line,
			                 hostname=_name(name, answer, prompt))
	return Detection()


def _name(found: str, answer: str, prompt: str) -> str | None:
	""":returns: the device's name - where its platform's answer says it (a
	 prompt can be set to anything: IOS's "prompt" command), else from its
	 prompt; None when neither says"""
	platform = PLATFORMS.get(found)
	if platform and platform.answer_name and (named := platform.answer_name.search(answer)):
		return named.group("name")
	return hostname(platform.prompt_name if platform else NAME_CISCO, prompt)


def probes(prompt: str, known: dict[str, Identity]) -> list[tuple[str, str]]:
	"""The commands to send, in order, each with the probe it asks: first a
	probe through the shell the prompt shows (Gaia's expert, Junos root's
	Unix shell), then the probes the prompt's shape suggests, then every
	probe in PLATFORMS' order - each once. The order only saves commands: an
	answer is claimed the same whichever comes first.

	:returns: [(command, probe)]"""
	order: list[tuple[str, str]] = []
	for identity in known.values():
		if identity.shell and identity.shell.prompt.match(prompt):
			order.append((identity.shell.run.format(identity.probe), identity.probe))
	hinted = [i.probe for i in known.values() if i.prompt and i.prompt.match(prompt)]
	for probe in hinted + [i.probe for i in known.values()]:
		if (probe, probe) not in order:
			order.append((probe, probe))
	return order


def ask(conn: Channel, command: str, prompt: str, until: float,
        clock: Callable[[], float], pause: Callable[[float], None]) -> str:
	"""Send one command and read its answer until the prompt is back (or the
	time is up: what came so far), a pager on the way left with its key.

	:returns: the answer, the echo and the prompt included"""
	conn.write_channel(command + conn.RETURN)
	answer = ""
	while clock() < until:
		chunk = conn.read_channel()
		if not chunk:
			pause(_POLL)
			continue
		answer += _ESCAPES.sub("", chunk)
		tail = answer.rstrip()
		for pager, key in PAGERS:
			if found := pager.search(tail):
				conn.write_channel(key)
				answer = tail[:found.start()]
				break
		else:
			if tail.endswith(prompt):
				return answer
	return answer
