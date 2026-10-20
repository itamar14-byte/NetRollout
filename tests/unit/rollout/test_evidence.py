"""The judge (src/rollout/evidence.py) on the configs recorded from the lab
(tests/unit/rollout/lab: R1 IOS 12.4, NXOS1 NX-OS 7.3, FGT1 FortiOS 7.2 -
full before / after snapshots, each command's raw reply, a second after
snapshot; secrets replaced by consistent <secret-N> placeholders): a verdict
is certain only where the device's own configuration proves it, everything
else is review - with no keyword, no threshold, no vendor rewrite rule."""
import difflib
import gzip
import json
from pathlib import Path

import pytest

from src.rollout.engine import diff_tags, transcript
from src.rollout.evidence import Verdict, compare, contains, judge, matches, noise, parse, same_word
from src.rollout.platforms import PLATFORMS

LAB = Path(__file__).parent / "lab"
T, P, R, N, V = Verdict.TOOK_EFFECT, Verdict.IN_PLACE, Verdict.REMOVED, Verdict.NAVIGATION, Verdict.REVIEW


def recorded(case):
	with gzip.open(LAB / f"{case}.json.gz", "rt", encoding="utf-8") as f:
		return json.load(f)


def judged(case):
	r = recorded(case)
	return r, judge(r["commands"], r["before"], r["after"], blocks=r["device_type"] == "fortinet",
	                again=r["after_again"])


# (case, the verdict of each command in order, the evidence's first line - None: no evidence)
CASES = [
	("R1/acl-new", [T, T, T, N], ["+ ip access-list extended NR-TEST", "+ permit tcp any any eq 22", "+ deny   ip any any", None]),
	("R1/acl-remove", [R], ["- ip access-list extended NR-TEST"]),
	("R1/abbreviated-interface", [T, T, N], ["+ interface Loopback98", "+ ip address 10.98.98.1 255.255.255.255", None]),
	("R1/rerun-unchanged", [P, P, N], ["  interface Loopback98", "  ip address 10.98.98.1 255.255.255.255", None]),
	("R1/interface-remove", [R], ["- interface Loopback98"]),
	("R1/value-new", [T], ["+ snmp-server location A"]),
	("R1/value-replaced", [T], ["+ snmp-server location B"]),
	("R1/value-remove", [R], ["- snmp-server location B"]),
	("R1/secret-hashed", [V], None),
	("R1/refused-ntp", [V], [None]),
	("R1/typo", [V], [None]),
	("R1/token-two-lines", [T, T, T, N], ["+ interface Loopback97", "+ description nr-token-test",
	                                      "+ ip address 10.97.97.1 255.255.255.255", None]),
	("NXOS1/mask-rewritten", [T, V, N], ["+ interface loopback98", "+ ip address 10.98.98.2/32", None]),
	("NXOS1/rerun-rewritten", [P, V, N], ["  interface loopback98", None, None]),
	("NXOS1/interface-remove", [R], ["- interface loopback98"]),
	("NXOS1/acl-sequence-numbers", [T, T, T, N], ["+ ip access-list NR-TEST", "+ 10 permit tcp any any eq 22",
	                                              "+ 20 deny ip any any", None]),
	("NXOS1/vlan-merged", [T, N], ["+ vlan 1,98", None]),
	("NXOS1/value-new", [T], ["+ snmp-server location A"]),
	("NXOS1/typo", [V], [None]),
	("FGT1/named-entry", [P, T, T, N, N], ["  config firewall address", '+ edit "nr-test"',
	                                       "+ set subnet 10.98.98.0 255.255.255.0", None, None]),
	("FGT1/named-entry-delete", [P, R, N], ["  config firewall address", '- edit "nr-test"', None]),
	("FGT1/edit-0-renumbered", [T, T, T, T, T, T, T, T, T, T, N, N], None),
	("FGT1/value-non-default", [P, T, N], ["  config system global", "+ set admintimeout 30", None]),
	("FGT1/value-back-to-default", [P, V, N], ["  config system global", "- set admintimeout 30", None]),
	("FGT1/value-unset", [P, T, N], ["  config system global", '+ set alias "nr-alias"', None]),
	("FGT1/unset", [P, R, N], ["  config system global", '- set alias "nr-alias"', None]),
	("FGT1/typo", [V], [None]),
]


@pytest.mark.parametrize("case, verdicts, evidence", CASES, ids=[c[0] for c in CASES])
def test_recorded_push_is_judged_by_its_own_configuration(case, verdicts, evidence):
	"""Each command's verdict, and the device line it rests on."""
	_, outcome = judged(case)
	assert [j.verdict for j in outcome.commands] == verdicts
	if evidence is not None:
		assert [j.evidence[0] if j.evidence else None for j in outcome.commands] == evidence


def test_a_device_needs_review_exactly_when_a_command_does():
	"""The device-level flag follows the commands: certain pushes don't need
	a person, any uncertain command does."""
	for case, verdicts, _ in CASES:
		assert judged(case)[1].needs_review == (V in verdicts), case


def test_fortios_renumbered_entry_is_named_as_the_device_named_it():
	"""FortiOS "edit 0" becomes "edit 1": the command took effect as that
	block, said so, and every "set" inside it is placed there."""
	_, outcome = judged("FGT1/edit-0-renumbered")
	assert outcome.commands[1].evidence == ("+ edit 1",)
	assert outcome.commands[1].note == "the device named it 'edit 1'"
	assert outcome.commands[2].evidence == ('+ set name "nr-test"',)


def test_uncertain_commands_show_the_section_changes_or_say_there_were_none():
	"""A review carries what changed in the command's section - or, when
	nothing did, says so (the refused NTP server, a typo)."""
	_, rewritten = judged("NXOS1/mask-rewritten")
	assert rewritten.commands[1].evidence == ("+ ip address 10.98.98.2/32",)
	_, refused = judged("R1/refused-ntp")
	assert refused.commands[0].evidence == () and refused.commands[0].note == "no change in its section"


def test_fortios_reencrypted_passwords_are_learned_as_noise():
	"""FortiOS re-encrypts every stored password on each read (44 lines in
	the lab): two snapshots in a row teach it, so they aren't reported as
	other changes - without the second snapshot they would be."""
	r = recorded("FGT1/named-entry")
	assert len(judge(r["commands"], r["before"], r["after"], blocks=True).other_changes) == 44
	assert judge(r["commands"], r["before"], r["after"], blocks=True, again=r["after_again"]).other_changes == ()


def test_learned_noise_is_section_and_leading_words():
	"""The churn is described by where it is and the words that stayed
	(FortiOS: "set password", the re-encrypted value - in the recording
	"ENC …" is one <secret-N> placeholder), not by any list."""
	r = recorded("FGT1/noise")
	churn = noise(parse(r["after"]), parse(r["after_again"]))
	assert churn and all(lead == ("set", "password") for _, lead in churn)


def test_a_replaced_value_is_one_change():
	"""snmp-server location A → B: the removed A is accounted for by the B
	that replaced it, not reported as someone else's change."""
	_, outcome = judged("R1/value-replaced")
	assert not any("location A" in change for change in outcome.other_changes)


def test_changes_inside_a_new_block_belong_to_it():
	"""FortiOS adds "set uuid …" to a new entry: part of the block the push
	opened, not an other change."""
	_, outcome = judged("FGT1/named-entry")
	assert not any("uuid" in change for change in outcome.other_changes)


@pytest.mark.parametrize("typed, device, same", [
	("int", "interface", True), ("lo98", "loopback98", True), ("gi1/0/1", "gigabitethernet1/0/1", True),
	("add", "address", True), ("ip", "ipv6", False), ("e1", "ethernet1/1", False), ("i", "interface", False),
	("10.1.1.1", "10.1.1.10", False), ("lo9", "loopback98", False),
])
def test_a_typed_word_matches_the_device_word_or_its_abbreviation(typed, device, same):
	"""Abbreviation is the CLI convention: the letters a prefix (2 or more),
	whatever follows them exactly equal."""
	assert same_word(typed, device) is same


def test_contains_allows_device_words_matches_does_not():
	"""Took effect allows words the device adds (a sequence number);
	already in place doesn't - an unchanged "ip address X M secondary" is
	not the command "ip address X M"."""
	assert contains(["permit", "tcp"], ["10", "permit", "tcp"])
	assert not matches(["ip", "address", "1.1.1.1", "255.0.0.0"], ["ip", "address", "1.1.1.1", "255.0.0.0", "secondary"])
	assert matches(["int", "lo98"], ["interface", "loopback98"])


def test_two_commands_claiming_one_change_both_go_to_review():
	"""One added line can't prove two different commands."""
	before = "hostname R1\n"
	after = "hostname R1\nsnmp-server location lab\n"
	outcome = judge(["snmp-server location lab", "snmp-server location"], before, after)
	assert [j.verdict for j in outcome.commands] == [V, V]


def test_a_removal_of_something_never_there_is_already_absent():
	"""Nothing to remove, before or after: already in place, said so."""
	outcome = judge(["no snmp-server location"], "hostname R1\n", "hostname R1\n")
	assert outcome.commands[0].verdict == P and outcome.commands[0].note == "already absent"


def test_flat_configs_extend_relative_commands_with_their_edit_path():
	"""Junos / PAN-OS style: "edit" sets the path a relative "set" extends."""
	before = "set system host-name r1\n"
	after = "set system host-name r1\nset interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/24\n"
	outcome = judge(["edit interfaces ge-0/0/0", "set unit 0 family inet address 10.0.0.1/24", "top"],
	                before, after, flat=True)
	assert [j.verdict for j in outcome.commands] == [N, T, N]


def test_compare_counts_repeated_lines():
	"""The same line twice in a section is two lines."""
	diff = compare(parse("a\na\n"), parse("a\n"))
	assert len(diff.removed) == 1 and len(diff.unchanged) == 1


def test_a_line_of_only_quotes_is_not_judged():
	"""A command of only double quotes normalizes to nothing: it isn't judged
	(navigation), and the commands around it still are."""
	outcome = judge(["hostname x", '""', '" "'], "hostname w\n", "hostname x\n")
	assert [j.verdict for j in outcome.commands] == [T, N, N]


def test_a_reviews_evidence_leaves_out_what_later_commands_took():
	"""Found on R1: a refused command followed, in the same rollout, by one that
	added a line in the same section showed that line as its evidence. The
	evidence is gathered once every command has claimed its lines."""
	before = "hostname r1\n"
	after = "hostname r1\ninterface Loopback96\n ip address 10.96.96.1 255.255.255.255\n"
	outcome = judge(["ntp server 192.0.2.123", "interface Loopback96",
	                 "ip address 10.96.96.1 255.255.255.255", "exit"], before, after)
	ntp = outcome.commands[0]
	assert (ntp.verdict, ntp.evidence, ntp.note) == (V, (), "no change in its section")
	assert [j.verdict for j in outcome.commands[1:]] == [T, T, N]


# ── Every changed line, with what made it (Results → Verification's diff) ──

def by(outcome, sign, text):
	""":returns: the one change of `text` (sign "+" / "-")"""
	(change,) = [c for c in outcome.changes if c.sign == sign and c.line == text]
	return change


def test_each_changed_line_names_the_command_it_rests_on():
	"""R1 snmp-server location A → B: the new line rests on the command, and so
	does the removed A it replaced."""
	r, outcome = judged("R1/value-replaced")
	assert by(outcome, "+", "snmp-server location B").by == (0,)
	assert by(outcome, "-", "snmp-server location A").by == (0,)


def test_lines_inside_a_new_block_belong_to_the_command_that_opened_it():
	"""FortiOS's own "set uuid" in a new entry is tagged with the "edit" that
	opened it."""
	r, outcome = judged("FGT1/named-entry")
	(uuid,) = [c for c in outcome.changes if c.line.startswith("set uuid")]
	assert uuid.by == (1,) and not uuid.device


def test_a_rewritten_line_is_tagged_as_the_evidence_of_its_review():
	"""NX-OS's "/32" sits in the block "interface loopback98" opened, and is the
	evidence of the "ip address … 255.255.255.255" command in review."""
	r, outcome = judged("NXOS1/mask-rewritten")
	line = by(outcome, "+", "ip address 10.98.98.2/32")
	assert line.by == (0,) and line.review == (1,)
	assert line.section == "interface loopback98 > "


def test_the_devices_own_churn_is_kept_apart_not_dropped():
	"""FortiOS's 44 re-encrypted passwords are in the changes as the device's
	own (shown behind a switch), never as another person's change."""
	r, outcome = judged("FGT1/named-entry")
	churn = [c for c in outcome.changes if c.device]
	assert len(churn) == 44 and all(not c.by for c in churn)
	assert outcome.other_changes == ()


def test_other_changes_are_exactly_the_lines_nobody_made():
	"""Across the whole lab corpus, "changed, not by these commands" is the
	changes with no command and not the device's."""
	for case, _, _ in CASES:
		_, outcome = judged(case)
		assert outcome.other_changes == tuple(
			f"{c.sign} {c.section}{c.line}" for c in outcome.changes if not c.by and not c.device), case


def test_the_stored_diff_finds_every_lines_tag_secrets_included():
	"""The Verification window's diff (stored as the device printed it) gets
	every configuration line's tag from the transcript (stored redacted): FortiOS's
	44 re-encrypted "set password" lines are found as the device's own, the
	new entry's lines as its commands'."""
	r = recorded("FGT1/named-entry")
	platform = PLATFORMS["fortinet"]
	before, after = platform.config(r["before"]), platform.config(r["after"])
	outcome = judge(r["commands"], before, after, blocks=True, again=platform.config(r["after_again"]))
	diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
	                                      "before", "after", n=3, lineterm=""))
	tags = diff_tags(diff, transcript(outcome, ()))
	changed = [(line, tag) for line, tag in zip(diff.splitlines(), tags)
	           if line[:1] in "+-" and line[:3] not in ("+++", "---")]
	# the grammar's skipped lines aren't configuration: no tag (the file's
	# version comment, the block's closing "next")
	assert [line for line, tag in changed if tag is None] == [
		"-#conf_file_ver=512419663302546", "+#conf_file_ver=512432548204437", "+    next"]
	assert sum(1 for _, tag in changed if tag and tag["device"]) == 44
	assert [tag["by"] for line, tag in changed if tag and "set subnet" in line] == [[2]]


def test_a_line_repeated_in_two_sections_takes_its_own_tag():
	"""The same text changed in two sections: each diff line takes the next
	change of that text, in order."""
	stored = {"changes": [{"sign": "+", "line": "shutdown", "section": "interface a > ", "by": [1],
	                       "review": [], "device": False},
	                      {"sign": "+", "line": "shutdown", "section": "interface b > ", "by": [3],
	                       "review": [], "device": False}]}
	diff = "--- before\n+++ after\n@@ -1 +1 @@\n interface a\n+ shutdown\n interface b\n+ shutdown"
	tags = diff_tags(diff, stored)
	assert [t and t["by"] for t in tags] == [None, None, None, None, [1], None, [3]]


def test_a_transcript_from_before_the_tags_gives_none():
	"""A row judged before the changes were kept: no tags (the plain diff)."""
	assert diff_tags("+ x", {"commands": [], "other_changes": []}) is None
