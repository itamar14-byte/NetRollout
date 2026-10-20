"""Judging a push by the device itself: its configuration before and after
the commands, compared - no I/O. What the comparison proves is reported as
fact; what it doesn't prove goes to a person (review). What the device
printed is kept as the reason (reply), never used as evidence.

The only platform knowledge used here is grammar: how a config is laid out
(indented sections, FortiOS blocks, flat "set" lines), which command words
open or leave a block, how a removal is written. Never what a message means,
never how a device rewrites a value. The design and its evidence from the
lab: docs/plans/verification-rework.md."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

# Leave or close a config section - they configure nothing
NAVIGATION = {"exit", "end", "next", "abort", "quit", "return", "top", "up",
              "root",                   # IOS-XR: back to the top of config
              "end-policy", "end-set"}  # IOS-XR: close route-policy / sets
# A removal: "no X" (Cisco-style), "undo X" (Comware), "unset X" /
# "delete X" (FortiOS, Junos, PAN-OS, Gaia)
NEGATION = {"no", "undo", "unset", "delete"}
# A line the device prints between commands that is a time stamp, not an
# answer: IOS "*Oct 19 19:17:26.223: …", NX-OS "2026 Oct 19 16:10:28 …"
_EVENT = re.compile(r"^\*?(\d{4} )?[A-Z][a-z]{2} +\d+ (\d{4} )?\d\d:\d\d:\d\d")
# Junos's context banner after a command ("[edit interfaces ge-0/0/0]")
_CONTEXT = re.compile(r"^\[edit( .*)?\]$")
_WORD = re.compile(r"([a-z]*)(.*)", re.S)


class Verdict(StrEnum):
	"""What the configuration says about one command (stored in a device's
	transcript; its values never change)."""
	TOOK_EFFECT = "took effect"      # in a line the push added
	IN_PLACE = "already in place"    # already there (a removal: already absent)
	REMOVED = "removed"              # a removal, in a line the push removed
	NAVIGATION = "navigation"        # opens/leaves nothing judged (exit, end…)
	REVIEW = "review"                # not proven either way: a person decides


def normalize(line: str) -> str:
	""":returns: a config line or command as compared - spacing, case and
	 FortiOS / ProCurve quoting don't change the meaning"""
	return " ".join(line.replace('"', "").split()).lower()


def first_word(command: str) -> str:
	""":returns: a command's first word as compared ("" when normalizing
	 leaves nothing - a blank line, or one of only quotes)"""
	return (normalize(command).split() or [""])[0]


def navigates(word: str) -> bool:
	""":returns: whether a command's first word only moves between config
	 modes (exit, end, quit, … - also IOS exit-address-family, exit-vrf)"""
	return word in NAVIGATION or word.startswith("exit-")


def words(text: str) -> list[str]:
	""":returns: a line's words as compared: normalized, and a comma list is
	 several words (NX-OS "vlan 1,98")"""
	return [w for w in re.split(r"[\s,]+", normalize(text)) if w]


def same_word(typed: str, device: str) -> bool:
	"""Whether a typed word is the device's word: equal, or an abbreviation
	of it - its letters a prefix of the device's (2 or more) and whatever
	follows them (a number, an interface index) exactly equal: "int" →
	"interface", "lo98" → "loopback98", "gi1/0/1" → "gigabitethernet1/0/1";
	never "ip" → "ipv6", "e1" → "ethernet1/1", nor any number or address."""
	if typed == device:
		return True
	t_letters, t_rest = _split(typed)
	d_letters, d_rest = _split(device)
	return len(t_letters) >= 2 and d_letters.startswith(t_letters) and t_rest == d_rest


def _split(word: str) -> tuple[str, str]:
	match = _WORD.match(word)
	assert match is not None    # the pattern matches any string
	return match.group(1), match.group(2)


def contains(command: list[str], line: list[str]) -> bool:
	""":returns: whether all the command's words appear in the line, in
	 order (the device may add its own: a sequence number, a "secondary")"""
	at = 0
	for typed in command:
		while at < len(line) and not same_word(typed, line[at]):
			at += 1
		if at == len(line):
			return False
		at += 1
	return True


def matches(command: list[str], line: list[str]) -> bool:
	""":returns: whether the line is exactly the command (abbreviations
	 allowed, no word added) - what "already in place" requires"""
	return len(command) == len(line) and all(map(same_word, command, line))


@dataclass(frozen=True)
class Line:
	"""One config line: the section headers above it and its own text
	(normalized), and the text as the device printed it."""
	path: tuple[str, ...]
	text: str
	raw: str

	@property
	def key(self) -> tuple[tuple[str, ...], str]:
		return self.path, self.text

	@property
	def words(self) -> list[str]:
		return words(self.text)

	@property
	def inside(self) -> tuple[str, ...]:
		""":returns: the path of the lines under this one"""
		return (*self.path, self.text)


def parse(config: str, flat: bool = False) -> list[Line]:
	"""The config as lines with their sections. Indented configs nest by
	indentation; comment lines ('!', '#') are left out, at column 0 also
	closing any open section (Comware indents its top-level lines after
	'#'); block-closing lines (FortiOS "next" / "end") are left out. A flat
	config (one full "set …" line per setting) has no sections.

	:param flat: Junos display set, PAN-OS set format, Gaia"""
	lines: list[Line] = []
	stack: list[tuple[int, str]] = []
	for row in config.splitlines():
		raw = row.strip()
		if not raw:
			continue
		if raw[0] in "!#":
			if not row[0].isspace():
				stack = []
			continue
		if flat:
			lines.append(Line((), normalize(raw), raw))
			continue
		indent = len(row) - len(row.lstrip())
		while stack and stack[-1][0] >= indent:
			stack.pop()
		text = normalize(raw)
		if navigates(first_word(raw)):
			continue
		lines.append(Line(tuple(t for _, t in stack), text, raw))
		stack.append((indent, text))
	return lines


@dataclass
class Diff:
	"""Two snapshots of one config compared, line by line within sections
	(the same line twice in a section counts twice)."""
	added: list[Line]
	removed: list[Line]
	unchanged: list[Line]


def compare(before: list[Line], after: list[Line]) -> Diff:
	""":returns: the lines the second snapshot added, the ones it removed,
	 and the ones in both"""
	remaining = Counter(line.key for line in before)
	added, unchanged = [], []
	for line in after:
		if remaining[line.key]:
			remaining[line.key] -= 1
			unchanged.append(line)
		else:
			added.append(line)
	kept = Counter(line.key for line in unchanged)
	removed = []
	for line in before:
		if kept[line.key]:
			kept[line.key] -= 1
		else:
			removed.append(line)
	return Diff(added, removed, unchanged)


Noise = set[tuple[tuple[str, ...], tuple[str, ...]]]


def noise(after: list[Line], again: list[Line]) -> Noise:
	"""The device's own churn, learned from two snapshots taken one after
	the other with nothing pushed in between: each changed line, as its
	section and the words it kept (FortiOS re-encrypts every "set password
	ENC …" on each read: section + "set password enc"). A line changed
	without a counterpart is noise as a whole.

	:returns: (section, leading words) pairs; a change of a line that starts
	 with them, in that section, is the device's own"""
	diff = compare(after, again)
	found: Noise = set()
	for gone in diff.removed:
		best: tuple[str, ...] = tuple(gone.words)
		longest = 0
		for new in diff.added:
			if new.path != gone.path:
				continue
			common = _common_prefix(gone.words, new.words)
			if len(common) > longest:
				best, longest = tuple(common), len(common)
		found.add((gone.path, best))
	for new in diff.added:
		if not any(new.path == path and new.words[:len(lead)] == list(lead)
		           for path, lead in found):
			found.add((new.path, tuple(new.words)))
	return found


def _common_prefix(a: list[str], b: list[str]) -> list[str]:
	common = []
	for x, y in zip(a, b):
		if x != y:
			break
		common.append(x)
	return common


def _is_noise(line: Line, churn: Noise) -> bool:
	return any(line.path == path and line.words[:len(lead)] == list(lead)
	           for path, lead in churn)


@dataclass(frozen=True)
class Judgment:
	"""One command's verdict with its evidence: the device's lines it rests
	on ("+ " added, "- " removed, "  " unchanged), or why there are none."""
	command: str
	verdict: Verdict
	evidence: tuple[str, ...] = ()
	note: str = ""


@dataclass(frozen=True)
class Outcome:
	"""A device's judged push: a verdict per command, in order, and the
	changes no command explains (another person, or the device itself)."""
	commands: tuple[Judgment, ...]
	other_changes: tuple[str, ...]

	@property
	def needs_review(self) -> bool:
		return any(j.verdict == Verdict.REVIEW for j in self.commands)


class _Judge:
	"""One device's push judged against its two snapshots (judge())."""

	def __init__(self, before: list[Line], after: list[Line],
	             churn: Noise) -> None:
		diff = compare(before, after)
		self.added = [line for line in diff.added if not _is_noise(line, churn)]
		self.removed = [line for line in diff.removed if not _is_noise(line, churn)]
		self.unchanged = diff.unchanged
		self.before, self.after = before, after
		# sections that have lines under them: a line opening one is a header
		self.sections = {line.path for line in after} | {line.path for line in before}
		# claims: line index in added / removed → the commands that rest on it
		self.claims_added: dict[int, list[int]] = {}
		self.claims_removed: dict[int, list[int]] = {}

	def is_header(self, line: Line) -> bool:
		return line.inside in self.sections

	def levels(self, context: list[str] | None) -> list[tuple[str, ...] | None]:
		""":returns: where to look for a command: its section, then each
		 outer one (IOS leaves a section for a command it finds only
		 outside); None: anywhere (the section is unknown)"""
		if context is None:
			return [None]
		return [tuple(context[:level]) for level in range(len(context), -1, -1)]

	@staticmethod
	def at(line: Line, path: tuple[str, ...] | None) -> bool:
		return path is None or line.path == path

	def find_added(self, command: list[str], path: tuple[str, ...] | None) -> int | None:
		for index, line in enumerate(self.added):
			if self.at(line, path) and contains(command, line.words):
				return index
		return None

	def find_in_place(self, command: list[str], path: tuple[str, ...] | None) -> Line | None:
		for line in self.unchanged:
			if self.at(line, path) and matches(command, line.words):
				return line
		return None

	def find_removed(self, target: list[str], path: tuple[str, ...] | None) -> int | None:
		for index, line in enumerate(self.removed):
			if self.at(line, path) and contains(target, line.words):
				return index
		return None

	def anywhere(self, target: list[str], path: tuple[str, ...] | None) -> bool:
		""":returns: whether a line with the target's words is in either
		 snapshot (a removal is "already absent" only if none is)"""
		return any(self.at(line, path) and contains(target, line.words)
		           for line in self.before + self.after)

	def opened_block(self, path: tuple[str, ...] | None,
	                 following: list[str] | None) -> int | None:
		"""A command that matched nothing may have opened a block the device
		named its own way (FortiOS "edit 0" → "edit 1"): the unclaimed new
		header in its section whose new lines hold the next command.

		:returns: that header's index in added, or None"""
		if not following:
			return None
		for index, header in enumerate(self.added):
			if index in self.claims_added or not self.at(header, path) \
					or not self.is_header(header):
				continue
			if any(line.path == header.inside and contains(following, line.words)
			       for line in self.added):
				return index
		return None

	def changes_in(self, path: tuple[str, ...] | None) -> tuple[str, ...]:
		""":returns: the section's changes, as evidence for a review"""
		return tuple([f"+ {line.raw}" for line in self.added if self.at(line, path)][:10]
		             + [f"- {line.raw}" for line in self.removed if self.at(line, path)][:10])

	def explained(self) -> tuple[set[int], set[int]]:
		""":returns: the added and removed lines the commands account for:
		 their own, everything under a block they opened or removed, and a
		 line a claimed one replaced (the same section and leading words:
		 "snmp-server location a" → "… b", "vlan 1" → "vlan 1,98")"""
		blocks = {self.added[i].inside for i in self.claims_added} | \
			{self.removed[i].inside for i in self.claims_removed}
		under = lambda line: any(line.path[:len(b)] == b for b in blocks)   # noqa: E731
		added = {i for i, line in enumerate(self.added)
		         if i in self.claims_added or under(line)}
		removed = {i for i, line in enumerate(self.removed)
		           if i in self.claims_removed or under(line)}
		for i, gone in enumerate(self.removed):
			if i in removed:
				continue
			for j in self.claims_added:
				new = self.added[j]
				common = _common_prefix(gone.words, new.words)
				if new.path == gone.path and (len(common) >= 2 or common == gone.words):
					removed.add(i)
					break
		return added, removed


def judge(commands: list[str], before: str, after: str, *, flat: bool = False,
          blocks: bool = False, again: str | None = None) -> Outcome:
	"""Judge a push by the device's configuration before and after it.

	Each command is placed in its section by the commands before it (a
	header opens one, navigation leaves it), then:

	- took effect: its words are all in a line the push added there, in
	  order (the device may add words of its own);
	- already in place: a line there, unchanged, is exactly the command;
	  a removal: no line there has its words, before or after;
	- removed: a removal ("no …", "undo …", "unset …", "delete …") whose
	  words are in a line the push removed there;
	- a command that matched nothing but whose next command is in a new
	  block the device named its own way took effect as that block's header
	  (FortiOS "edit 0" → "edit 1");
	- review: anything else, or one added line claimed by two different
	  commands - with the section's changes as evidence.

	:param commands: as sent to this device (tokens resolved)
	:param flat: one full "set …" line per setting (PLATFORMS' flat)
	:param blocks: FortiOS - "end" closes the current config block only
	:param again: a second snapshot taken right after the after one: its
	 differences from it are the device's own churn, left out
	:returns: the verdicts, and the changes no command explains"""
	b, a = parse(before, flat), parse(after, flat)
	churn = noise(a, parse(again, flat)) if again is not None else set()
	j = _Judge(b, a, churn)
	results: list[Judgment] = []
	context: list[str] | None = []
	resolved = _flat_all(commands) if flat else [(c, words(c)) for c in commands]
	for i, (command, typed) in enumerate(resolved):
		word = typed[0] if typed else ""
		if not typed or typed == ["#nav"] or (navigates(word) and not flat):
			if not flat and typed:
				leaves_all = word in ("return", "root") or (word == "end" and not blocks)
				if leaves_all:
					context = []
				elif context:
					context = context[:-1]
			results.append(Judgment(command, Verdict.NAVIGATION))
			continue
		if word in NEGATION:
			results.append(_judge_removal(j, i, command, typed[1:], context))
			continue
		following = next((w for _, w in resolved[i + 1:]
		                  if w and not navigates(w[0]) and w != ["#nav"]), None)
		judgment, context = _judge_setting(j, i, command, typed, context, following)
		results.append(judgment)
	# one added line resting on two different commands proves neither
	for index, claimants in j.claims_added.items():
		if len({normalize(commands[c]) for c in claimants}) > 1:
			for c in claimants:
				results[c] = Judgment(commands[c], Verdict.REVIEW, (f"+ {j.added[index].raw}",),
				                      "the same change matches another command too")
	added, removed = j.explained()
	other = tuple([f"+ {_where(line)}{line.raw}" for i, line in enumerate(j.added) if i not in added]
	              + [f"- {_where(line)}{line.raw}" for i, line in enumerate(j.removed) if i not in removed])
	return Outcome(tuple(results), other)


def _where(line: Line) -> str:
	return f"{' > '.join(line.path)} > " if line.path else ""


def _judge_setting(j: _Judge, i: int, command: str, typed: list[str],
                   context: list[str] | None, following: list[str] | None
                   ) -> tuple[Judgment, list[str] | None]:
	for path in j.levels(context):
		index = j.find_added(typed, path)
		if index is not None:
			j.claims_added.setdefault(index, []).append(i)
			line = j.added[index]
			return (Judgment(command, Verdict.TOOK_EFFECT, (f"+ {line.raw}",)),
			        _enter(j, line, context, path))
		kept = j.find_in_place(typed, path)
		if kept is not None:
			return (Judgment(command, Verdict.IN_PLACE, (f"  {kept.raw}",)),
			        _enter(j, kept, context, path))
	here = tuple(context) if context is not None else None
	index = j.opened_block(here, following)
	if index is not None:
		j.claims_added.setdefault(index, []).append(i)
		header = j.added[index]
		return (Judgment(command, Verdict.TOOK_EFFECT, (f"+ {header.raw}",),
		                 f"the device named it {header.raw!r}"), list(header.inside))
	changes = j.changes_in(here)
	return (Judgment(command, Verdict.REVIEW, changes,
	                 "" if changes else "no change in its section"), context)


def _enter(j: _Judge, line: Line, context: list[str] | None,
           path: tuple[str, ...] | None) -> list[str] | None:
	""":returns: the context after a command matched `line` at `path`: inside
	 it when it's a header, else the level where it was found"""
	if j.is_header(line):
		return list(line.inside)
	return list(line.path) if path is None else list(path)


def _judge_removal(j: _Judge, i: int, command: str, target: list[str],
                   context: list[str] | None) -> Judgment:
	for path in j.levels(context):
		index = j.find_removed(target, path)
		if index is not None:
			j.claims_removed.setdefault(index, []).append(i)
			return Judgment(command, Verdict.REMOVED, (f"- {j.removed[index].raw}",))
	here = tuple(context) if context is not None else None
	if target and not any(j.anywhere(target, path) for path in j.levels(context)):
		return Judgment(command, Verdict.IN_PLACE, (), "already absent")
	changes = j.changes_in(here)
	return Judgment(command, Verdict.REVIEW, changes,
	                "" if changes else "no change in its section")


def _flat_all(commands: list[str]) -> list[tuple[str, list[str]]]:
	"""Flat configs: "edit" / "up" / "top" move the prefix that relative
	"set" / "delete" commands extend; navigation becomes ["#nav"].

	:returns: (command, its full words) in order"""
	prefix: list[list[str]] = []
	out: list[tuple[str, list[str]]] = []
	for command in commands:
		typed = words(command)
		word = typed[0] if typed else ""
		if word == "edit":
			prefix.append(typed[1:])
			out.append((command, ["#nav"]))
		elif word in ("top", "end"):
			prefix = []
			out.append((command, ["#nav"]))
		elif navigates(word):
			prefix = prefix[:-1]
			out.append((command, ["#nav"]))
		elif word in ("set", "delete"):
			out.append((command, [word, *[w for level in prefix for w in level], *typed[1:]]))
		else:
			out.append((command, typed))
	return out


def reply(output: str, command: str, prompt: str) -> tuple[list[str], list[str]]:
	"""What the device said to one command, separated by shape from what
	isn't an answer: the command's echo, the prompt (with any context:
	"R1(config-if)#", "FGT1 (global) #"), blank lines, IOS's "^" pointer,
	Junos's "[edit …]" banner. Time-stamped lines are device events.

	:returns: (the reply's lines, the event lines) - never judged, shown"""
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
