"""Judging a push by the device itself: its configuration before and after
the commands, compared - no I/O. What the comparison proves is reported as
fact. A command it doesn't prove is rejected when the device replied to it
(whether it replied counts, its wording never), else it goes to a person
(review): NetRollout says what it knows and says when it can't know.

The only platform knowledge used here is grammar: how a config is laid out
(indented sections, FortiOS blocks, flat "set" lines), which command words
open or leave a block, how a removal is written. Never what a message means,
never how a device rewrites a value. The design and its evidence from the
lab: docs/plans/verification-rework.md."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

# Leave or close a config section - they configure nothing (every
# platform's; each platform declares its own - PLATFORMS' navigation)
NAVIGATION = {"exit", "end", "next", "abort", "quit", "return", "top", "up",
              "root",                   # IOS-XR: back to the top of config
              "end-policy", "end-set", "end-group"}  # IOS-XR: close route-policy / sets / groups
# A removal: "no X" (Cisco-style), "undo X" (Comware), "unset X" /
# "delete X" (FortiOS, Junos, PAN-OS, Gaia) - every platform's; each
# platform declares its own (PLATFORMS' negation)
NEGATION = {"no", "undo", "unset", "delete"}
_WORD = re.compile(r"([a-z]*)(.*)", re.S)


class Verdict(StrEnum):
	"""What the configuration says about one command (stored in a device's
	transcript; its values never change)."""
	TOOK_EFFECT = "took effect"      # in a line the push added
	IN_PLACE = "already in place"    # already there (a removal: already absent)
	REMOVED = "removed"              # a removal, in a line the push removed
	NAVIGATION = "navigation"        # opens/leaves nothing judged (exit, end…)
	REVIEW = "review"                # not proven either way: a person decides
	REJECTED = "rejected"            # not proven, and the device replied to it
	EXEC = "not a configuration command"  # an exec command (do show…): shown


def normalize(line: str) -> str:
	""":returns: a config line or command as compared - spacing, case and
	 FortiOS / ProCurve quoting don't change the meaning"""
	return " ".join(line.replace('"', "").split()).lower()


def first_word(command: str) -> str:
	""":returns: a command's first word as compared ("" when normalizing
	 leaves nothing - a blank line, or one of only quotes)"""
	return (normalize(command).split() or [""])[0]


def navigates(word: str, navigation: Collection[str] = NAVIGATION) -> bool:
	""":param navigation: the platform's navigation words
	:returns: whether a command's first word only moves between config
	 modes (exit, end, quit, … - also IOS exit-address-family, exit-vrf)"""
	return word in navigation or word.startswith("exit-")


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


def parse(config: str, flat: bool = False, navigation: Collection[str] = NAVIGATION) -> list[Line]:
	"""The config as lines with their sections. Indented configs nest by
	indentation; comment lines ('!', '#') are left out, at column 0 also
	closing any open section (Comware indents its top-level lines after
	'#'); block-closing lines (FortiOS "next" / "end") are left out. A flat
	config (one full "set …" line per setting) has no sections.

	:param flat: Junos display set, PAN-OS set format, Gaia
	:param navigation: the platform's navigation words (its block-closing
	 lines are left out)"""
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
		if navigates(first_word(raw), navigation):
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
# why two commands resting on one changed line are both in review
CONFLICT = "the same change matches another command too"
# a rejected command's note (what the device replied is shown beside it)
UNPROVEN = "not proven by the configuration"


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
	# where it was judged, its headers as the device printed them - for an
	# unproven command, the section a defaults fetch asks for (not stored)
	section: tuple[str, ...] | None = field(default=(), compare=False)


@dataclass(frozen=True)
class Change:
	"""One line the push changed in the configuration, and what made it:
	the commands it rests on (their positions in the push), nobody among
	them (another person), or the device itself (its own churn)."""
	sign: str                # "+" added, "-" removed
	line: str                # as the device prints it
	section: str             # its section headers, "a > b > " ("" at the top)
	by: tuple[int, ...] = ()
	device: bool = False
	review: tuple[int, ...] = ()   # the commands in review it's evidence for


@dataclass(frozen=True)
class Outcome:
	"""A device's judged push: a verdict per command, in order, and the
	changes no command explains (another person, or the device itself)."""
	commands: tuple[Judgment, ...]
	other_changes: tuple[str, ...]
	# the lines the commands rest on, as (section, line): what a save must keep
	added: tuple[tuple[tuple[str, ...], str], ...] = ()
	removed: tuple[tuple[tuple[str, ...], str], ...] = ()
	# every changed line, added then removed, then the device's churn
	changes: tuple[Change, ...] = ()

	@property
	def needs_review(self) -> bool:
		return any(j.verdict == Verdict.REVIEW for j in self.commands)

	@property
	def rejected(self) -> bool:
		return any(j.verdict == Verdict.REJECTED for j in self.commands)

	@property
	def unproven(self) -> list[tuple[str, ...]]:
		""":returns: the sections of the commands the configuration didn't
		 prove (review or rejected), once each - where a view with the
		 device's defaults may prove them (an unknown section: none)"""
		sections: list[tuple[str, ...]] = []
		for j in self.commands:
			if j.verdict in (Verdict.REVIEW, Verdict.REJECTED) and j.section is not None 					and j.section not in sections:
				sections.append(j.section)
		return sections


@dataclass(frozen=True)
class _Typed:
	"""A command as sent, its words as judged (flat configs: extended with
	the "edit" path), and whether it only moves between sections."""
	sent: str
	words: tuple[str, ...]
	navigation: bool
	exec_: bool = False


def _typed(commands: list[str], flat: bool, exec_words: Sequence[str] = (),
           navigation: Collection[str] = NAVIGATION) -> list[_Typed]:
	"""The commands as judged. Flat configs (Junos, PAN-OS): "edit" / "exit"
	/ "top" move the path that relative "set" / "delete" commands extend;
	"up" climbs hierarchy levels, which the path's words can't count - after
	it the path is unknown until "top", and relative commands are judged by
	their own words. A command after "top" / "up [N]" on the same line runs
	there and is judged.

	:param exec_words: the platform's exec commands' first words
	:param navigation: the platform's navigation words"""
	out: list[_Typed] = []
	prefix: list[tuple[str, ...]] | None = []
	for command in commands:
		typed = tuple(words(command))
		word = typed[0] if typed else ""
		if word and word in exec_words:
			out.append(_Typed(command, typed, False, True))
		elif not flat or not typed:
			out.append(_Typed(command, typed, not typed or navigates(word, navigation)))
		elif word in ("top", "up") and len(typed) > 1 and not (word == "up" and len(typed) == 2
		                                                         and typed[1].isdigit()):
			# "top set system host-name x", "up 2 activate system": the rest runs there
			rest = typed[2:] if word == "up" and typed[1].isdigit() else typed[1:]
			out.append(_Typed(command, rest, not rest))
		elif word == "edit":
			if prefix is not None:
				prefix.append(typed[1:])
			out.append(_Typed(command, typed, True))
		elif word in ("top", "end"):
			prefix = []
			out.append(_Typed(command, typed, True))
		elif word == "up":
			prefix = None
			out.append(_Typed(command, typed, True))
		elif navigates(word, navigation):
			prefix = prefix[:-1] if prefix is not None else None
			out.append(_Typed(command, typed, True))
		elif word in ("set", "delete"):
			path = tuple(w for level in prefix for w in level) if prefix is not None else ()
			out.append(_Typed(command, (word, *path, *typed[1:]), False))
		else:
			out.append(_Typed(command, typed, False))
	return out


Path = tuple[str, ...]


class _Judge:
	"""One device's push judged against its snapshots (judge()): the diff,
	which commands claimed which changed lines, and the section each
	command is in."""

	def __init__(self, before: list[Line], after: list[Line], churn: Noise,
	             blocks: bool, replied: Sequence[bool] = (),
	             view: list[Line] | None = None,
	             negation: Collection[str] = NEGATION) -> None:
		diff = compare(before, after)
		self.negation = negation
		self.replied = replied
		# the configuration shown with its defaults (None: not fetched)
		self.view = view or []
		self.added = [line for line in diff.added if not _is_noise(line, churn)]
		self.removed = [line for line in diff.removed if not _is_noise(line, churn)]
		self.churned = [("+", line) for line in diff.added if _is_noise(line, churn)] + \
			[("-", line) for line in diff.removed if _is_noise(line, churn)]
		self.unchanged = diff.unchanged
		self.both = before + after
		self.blocks = blocks
		# sections that have lines under them: a line opening one is a header
		# (the defaults view shows an empty one's: "interface loopback95")
		self.sections = {line.path for line in self.both + self.view}
		# a header's text as the device printed it, by the path it opens
		self.raw = {line.inside: line.raw for line in self.both}
		# claims: line index in added / removed → the commands resting on it
		self.claims_added: dict[int, list[int]] = {}
		self.claims_removed: dict[int, list[int]] = {}
		# where the command being judged is; None: unknown (anywhere)
		self.context: list[str] | None = []
		# commands in review → the section they were in (their evidence is
		# gathered once every command has claimed its lines)
		self.reviews: dict[int, Path | None] = {}
		# line index in added / removed → the commands in review it's
		# evidence for
		self.evidence_added: dict[int, list[int]] = {}
		self.evidence_removed: dict[int, list[int]] = {}

	def run(self, commands: list[_Typed]) -> Outcome:
		results: list[Judgment] = []
		for i, command in enumerate(commands):
			if command.exec_:
				results.append(Judgment(command.sent, Verdict.EXEC))
			elif command.navigation:
				self._navigate(command)
				results.append(Judgment(command.sent, Verdict.NAVIGATION))
			elif command.words[0] in self.negation:
				results.append(self._removal(i, command))
			else:
				following = next((c.words for c in commands[i + 1:]
				                  if not c.navigation and not c.exec_), None)
				results.append(self._setting(i, command, following))
		self._conflicts(results)
		if self.view:
			self._defaults(results, commands, self.view)
		self._review_evidence(results)
		self._replied(results, commands)
		changes = self._changes()
		other = tuple(f"{c.sign} {c.section}{c.line}" for c in changes
		              if not c.by and not c.device)
		return Outcome(tuple(results), other,
		               tuple(self.added[i].key for i in self.claims_added),
		               tuple(self.removed[i].key for i in self.claims_removed),
		               changes)

	def _navigate(self, command: _Typed) -> None:
		"""Leave one section, or all of them ("end" closes only the current
		block where blocks are FortiOS's)."""
		if not command.words:
			return
		word = command.words[0]
		if word in ("return", "root") or (word == "end" and not self.blocks):
			self.context = []
		elif self.context:
			self.context = self.context[:-1]

	def _levels(self) -> list[Path | None]:
		""":returns: where to look for a command: its section, then each
		 outer one (IOS leaves a section for a command it finds only
		 outside); None: anywhere (the section is unknown)"""
		if self.context is None:
			return [None]
		return [tuple(self.context[:level]) for level in range(len(self.context), -1, -1)]

	def _here(self) -> Path | None:
		return tuple(self.context) if self.context is not None else None

	@staticmethod
	def _at(line: Line, path: Path | None) -> bool:
		return path is None or line.path == path

	def _is_header(self, line: Line) -> bool:
		return line.inside in self.sections

	def _setting(self, i: int, command: _Typed, following: tuple[str, ...] | None) -> Judgment:
		typed = list(command.words)
		for path in self._levels():
			for index, line in enumerate(self.added):
				if self._at(line, path) and contains(typed, line.words):
					self.claims_added.setdefault(index, []).append(i)
					self._enter(line, path)
					return Judgment(command.sent, Verdict.TOOK_EFFECT, (f"+ {line.raw}",))
			for line in self.unchanged:
				if self._at(line, path) and matches(typed, line.words):
					self._enter(line, path)
					return Judgment(command.sent, Verdict.IN_PLACE, (f"  {line.raw}",))
		# the push removed its negation ("no ip domain lookup" gone: "ip
		# domain lookup" took effect - a default the device doesn't print)
		for path in self._levels():
			for index, line in enumerate(self.removed):
				if self._at(line, path) and line.words[:1] and line.words[0] in self.negation \
						and matches(typed, line.words[1:]):
					self.claims_removed.setdefault(index, []).append(i)
					return Judgment(command.sent, Verdict.TOOK_EFFECT, (f"- {line.raw}",),
					                "its negation was removed")
		opened = self._opened_block(following)
		if opened is not None:
			self.claims_added.setdefault(opened, []).append(i)
			header = self.added[opened]
			self.context = list(header.inside)
			return Judgment(command.sent, Verdict.TOOK_EFFECT, (f"+ {header.raw}",),
			                f"the device named it {header.raw!r}")
		return self._review(i, command)

	def _removal(self, i: int, command: _Typed) -> Judgment:
		"""A removal: a line it removed; or, where the device keeps the
		negation as a line of its own (IOS "no ip redirects"), that line
		added or already there; or nothing there to remove - certain only
		when the device said nothing (a mistyped removal finds nothing
		either, and the device objects)."""
		target = list(command.words[1:])
		typed = list(command.words)
		for path in self._levels():
			for index, line in enumerate(self.removed):
				if self._at(line, path) and contains(target, line.words):
					self.claims_removed.setdefault(index, []).append(i)
					return Judgment(command.sent, Verdict.REMOVED, (f"- {line.raw}",))
		for path in self._levels():
			for index, line in enumerate(self.added):
				if self._at(line, path) and contains(typed, line.words):
					self.claims_added.setdefault(index, []).append(i)
					return Judgment(command.sent, Verdict.TOOK_EFFECT, (f"+ {line.raw}",))
			for line in self.unchanged:
				if self._at(line, path) and matches(typed, line.words):
					return Judgment(command.sent, Verdict.IN_PLACE, (f"  {line.raw}",))
		if target and not self._said(i) and \
				not any(self._at(line, path) and contains(target, line.words)
				        for path in self._levels() for line in self.both):
			return Judgment(command.sent, Verdict.IN_PLACE, (), "already absent")
		return self._review(i, command)

	def _said(self, i: int) -> bool:
		""":returns: whether the device replied to command i"""
		return i < len(self.replied) and self.replied[i]

	def _review(self, i: int, command: _Typed) -> Judgment:
		""":returns: review - its evidence (what changed in its section that
		 no other command accounts for) is added at the end (_review_evidence)"""
		here = self._here()
		self.reviews[i] = here
		section = None if here is None else \
			tuple(self.raw.get(here[:k + 1], here[k]) for k in range(len(here)))
		return Judgment(command.sent, Verdict.REVIEW, section=section)

	def _review_evidence(self, results: list[Judgment]) -> None:
		"""Each command in review gets the changes in its section that no
		command claimed - or, when there are none, says so. (A block another
		command opened doesn't account for its lines here: NX-OS's rewritten
		"ip address …/32" inside a new interface is this command's evidence.)"""
		added = [i for i in range(len(self.added)) if i not in self.claims_added]
		removed = [i for i in range(len(self.removed)) if i not in self.claims_removed]
		for i, path in self.reviews.items():
			shown_added = [a for a in added if self._at(self.added[a], path)][:10]
			shown_removed = [r for r in removed if self._at(self.removed[r], path)][:10]
			for a in shown_added:
				self.evidence_added.setdefault(a, []).append(i)
			for r in shown_removed:
				self.evidence_removed.setdefault(r, []).append(i)
			changes = tuple([f"+ {self.added[a].raw}" for a in shown_added]
			                + [f"- {self.removed[r].raw}" for r in shown_removed])
			results[i] = Judgment(results[i].command, Verdict.REVIEW, changes,
			                      "" if changes else "no change in its section",
			                      section=results[i].section)

	def _enter(self, line: Line, path: Path | None) -> None:
		"""After a command matched `line`, found at `path`: inside it when it's
		a header, else at the level where it was found."""
		if self._is_header(line):
			self.context = list(line.inside)
		else:
			self.context = list(line.path) if path is None else list(path)

	def _opened_block(self, following: tuple[str, ...] | None) -> int | None:
		"""A command that matched nothing may have opened a block the device
		named its own way (FortiOS "edit 0" → "edit 1"): the unclaimed new
		header in its section whose new lines hold the next command.

		:returns: that header's index in added, or None"""
		if not following:
			return None
		here = self._here()
		for index, header in enumerate(self.added):
			if index in self.claims_added or not self._at(header, here) \
					or not self._is_header(header):
				continue
			if any(line.path == header.inside and contains(list(following), line.words)
			       for line in self.added):
				return index
		return None

	def _conflicts(self, results: list[Judgment]) -> None:
		"""One changed line resting on two different commands proves neither:
		both go to review."""
		for claims, lines, sign in ((self.claims_added, self.added, "+"),
		                            (self.claims_removed, self.removed, "-")):
			for index, claimants in claims.items():
				if len({normalize(results[c].command) for c in claimants}) > 1:
					for c in claimants:
						results[c] = Judgment(results[c].command, Verdict.REVIEW,
						                      (f"{sign} {lines[index].raw}",), CONFLICT,
						                      section=results[c].section)

	def _defaults(self, results: list[Judgment], commands: list[_Typed],
	              view: list[Line]) -> None:
		"""Commands the plain configuration didn't prove, looked up in the
		configuration shown with its defaults: a line there, in the
		command's section, that is exactly the command → in place (the
		configuration holds it now; whether the push caused it isn't
		claimed)."""
		for i, path in list(self.reviews.items()):
			if path is None or results[i].note == CONFLICT:
				continue
			typed = list(commands[i].words)
			line = next((line for line in view if line.path == path
			             and matches(typed, line.words)), None)
			if line is not None:
				del self.reviews[i]
				results[i] = Judgment(results[i].command, Verdict.IN_PLACE, (f"  {line.raw}",),
				                      "shown with the device's defaults")

	def _replied(self, results: list[Judgment], commands: list[_Typed]) -> None:
		"""What the configuration didn't prove, and the device replied to,
		is rejected - its reply's presence, never its wording. A reply to
		the command that closed a block (FortiOS "next" refusing a whole new
		entry) counts for the silent unproven commands since the previous
		one."""
		for i, j in enumerate(results):
			if j.verdict == Verdict.REVIEW and self._said(i):
				results[i] = Judgment(j.command, Verdict.REJECTED, j.evidence, UNPROVEN,
				                      section=j.section)
		start = 0
		for k, command in enumerate(commands):
			if not command.navigation:
				continue
			if self._said(k):
				for i in range(start, k):
					j = results[i]
					if j.verdict == Verdict.REVIEW:
						results[i] = Judgment(j.command, Verdict.REJECTED, j.evidence,
						                      f"{UNPROVEN}; the device replied when its block "
						                      f"closed ('{commands[k].sent.strip()}')", section=j.section)
			start = k + 1

	def _changes(self) -> tuple[Change, ...]:
		""":returns: every changed line with the commands that account for it
		 (and those in review it's evidence for).
		 Accounted for: a command's own line, everything under a block it
		 opened or removed, and a line a claimed one replaced (the same
		 section and leading words: "snmp-server location a" → "… b",
		 "vlan 1" → "vlan 1,98"). None of these: another person's change.
		 The device's churn last."""
		owners: dict[Path, set[int]] = {}
		for claims, lines in ((self.claims_added, self.added),
		                      (self.claims_removed, self.removed)):
			for index, who in claims.items():
				owners.setdefault(lines[index].inside, set()).update(who)

		def by_block(line: Line) -> set[int]:
			return {c for block, who in owners.items()
			        if line.path[:len(block)] == block for c in who}

		def replaced_by(gone: Line) -> set[int]:
			found: set[int] = set()
			for index, who in self.claims_added.items():
				new = self.added[index]
				common = _common_prefix(gone.words, new.words)
				if new.path == gone.path and (len(common) >= 2 or common == gone.words):
					found.update(who)
			return found

		changes = [Change("+", line.raw, _where(line),
		                  tuple(sorted(set(self.claims_added.get(index, ())) or by_block(line))),
		                  review=tuple(self.evidence_added.get(index, ())))
		           for index, line in enumerate(self.added)]
		changes += [Change("-", line.raw, _where(line),
		                   tuple(sorted(set(self.claims_removed.get(index, ()))
		                                or by_block(line) or replaced_by(line))),
		                   review=tuple(self.evidence_removed.get(index, ())))
		            for index, line in enumerate(self.removed)]
		changes += [Change(sign, line.raw, _where(line), device=True)
		            for sign, line in self.churned]
		return tuple(changes)


def judge(commands: list[str], before: str, after: str, *, flat: bool = False,
          blocks: bool = False, again: str | None = None,
          replied: Sequence[bool] = (), exec_words: Sequence[str] = (),
          defaults: str | None = None, navigation: Collection[str] = NAVIGATION,
          negation: Collection[str] = NEGATION) -> Outcome:
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
	- a negation the device keeps as a line ("no ip redirects") took effect
	  or is in place by that line; a setting took effect when the push
	  removed its negation ("no ip domain lookup" gone);
	- already absent (a removal): nothing there to remove, and the device
	  said nothing;
	- in place by the device's defaults: unproven, but exactly a line of its
	  section in the configuration shown with defaults;
	- rejected: unproven, and the device replied - to it, or to the command
	  that closed its block;
	- not a configuration command: the platform's exec words (do, get, …);
	- review: anything else, or one changed line claimed by two different
	  commands - with the section's changes as evidence.

	:param commands: as sent to this device (tokens resolved)
	:param flat: one full "set …" line per setting (PLATFORMS' flat)
	:param blocks: FortiOS - "end" closes the current config block only
	:param again: a second snapshot taken right after the after one: its
	 differences from it are the device's own churn, left out
	:param replied: per command, whether the device replied (platforms.reply
	 had lines for it); none given: silent
	:param exec_words: the platform's exec commands' first words
	:param defaults: the configuration shown with its defaults, for the
	 unproven commands' sections (Platform.defaults); None: not fetched
	:param navigation: the platform's navigation words
	:param negation: the platform's removal words
	:returns: the verdicts, and the changes no command explains"""
	b, a = parse(before, flat, navigation), parse(after, flat, navigation)
	churn = noise(a, parse(again, flat, navigation)) if again is not None else set()
	view = parse(defaults, flat, navigation) if defaults else None
	return _Judge(b, a, churn, blocks, replied, view, negation).run(
		_typed(commands, flat, exec_words, navigation))


def _where(line: Line) -> str:
	return f"{' > '.join(line.path)} > " if line.path else ""
