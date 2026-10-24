"""The judge on the whole lab corpus (tests/unit/rollout/lab: R1 IOS 12.4,
NXOS1 NX-OS 7.3, FGT1 FortiOS 7.2, PA1 PAN-OS 11.2, JS1 vJunos 24.4, XR1 IOS-XR 7.1), judged as a push session judges: what
the device said told apart by the platform's shapes, its exec words, and
its configuration shown with defaults where the case recorded it.

Every command carries the truth - set by the device's configuration, not by
what it printed: the recordings since 2026-10-21 hold it as "expected"
(worked / refused per command, None for navigation), the first 28 cases'
four refusals are listed here. Two guards: a refused command is never
accepted (the one direction that must never go wrong), and what is left
rejected or in review is exactly what's listed - more review is a
regression too (say something whenever the configuration can)."""
import gzip
import json
from pathlib import Path

import pytest

from src.rollout.evidence import Outcome, Verdict, judge
from src.rollout.platforms import PLATFORMS, reply

LAB = Path(__file__).parent / "lab"
# the first recordings (2026-10-20) carry no "expected": their refusals
FIRST_REFUSALS = {("R1/typo", 0), ("NXOS1/typo", 0), ("FGT1/typo", 0), ("R1/refused-ntp", 0)}
ACCEPTED = (Verdict.TOOK_EFFECT, Verdict.IN_PLACE, Verdict.REMOVED)

# worked, but rejected: the device replied and the configuration can't show it
KNOWN_FALSE_REJECTIONS = {
	# its effect (the keys) isn't kept in the configuration
	("R1/rsa-key-label", "crypto key generate rsa general-keys label NR-TEST modulus 512"),
	# the state is right (nothing to delete), but the device refused it
	("FGT1/delete-absent", "delete nr-absent"),
	("PA1/delete-absent", "delete address nr-b2-none"),     # "Object doesn't exist"
	("JS1/delete-absent", "delete snmp contact"),           # "statement not found"
	# recorded without the defaults view, which proves it (FGT1/defaults-same-warning)
	("FGT1/hidden-default-same", "set admin-sport 443"),
}
# worked, left for a person: the special cases the configuration can't decide
REVIEW = {
	# a value the device rewrites (/32, the new spelling) - not proven, said so
	("NXOS1/mask-rewritten", "ip address 10.98.98.2 255.255.255.255"),
	("NXOS1/rerun-rewritten", "ip address 10.98.98.2 255.255.255.255"),
	("NXOS1/defaults-rewritten", "ip address 10.98.98.2 255.255.255.255"),
	("XR1/address-rewritten", "ipv4 address 10.51.51.1/32"),         # stored as a mask
	("R1/deprecated-spelling", "ip domain-name nr.local"),
	# a secret stored hashed
	("R1/secret-hashed", "username nrtest secret Probe-Pass-1"),
	("NXOS1/user-password-hashed", "username nradv password Probe-Pass-1x role network-operator"),
	("FGT1/admin-password-hashed", "set password Probe-Pass-1x"),
	# undone by the next command: no trace either way
	("R1/syslog-datetime", "shutdown"),
	("R1/syslog-uptime", "shutdown"),
	# recorded without the defaults view, which proves them (FGT1/defaults-back-to-default,
	# NXOS1/defaults-noshut)
	("FGT1/value-back-to-default", "set admintimeout 5"),
	("NXOS1/hidden-default-noshut", "no shutdown"),
}


def judged(path: Path) -> tuple[dict, Outcome, list[list[str]]]:
	r = json.load(gzip.open(path, "rt", encoding="utf-8"))
	platform = PLATFORMS[r["device_type"]]
	said = [reply(out, cmd, r["prompt"], platform.events)[0] for out, cmd in zip(r["replies"], r["commands"])]
	defaults = "\n".join(platform.config(v) for v in r["defaults"].values()) if r.get("defaults") else None
	outcome = judge(r["commands"], platform.config(r["before"]), platform.config(r["after"]),
	                again=platform.config(r["after_again"]), replied=[bool(s) for s in said],
	                defaults=defaults, **platform.grammar)
	return r, outcome, said


def commands() -> list[tuple[str, str, str, Verdict, str, list[str]]]:
	""":returns: (case, command, truth, verdict, note, reply) for every judged command"""
	rows = []
	for path in sorted(LAB.glob("*/*.json.gz")):
		case = f"{path.parent.name}/{path.name[:-len('.json.gz')]}"
		r, outcome, said = judged(path)
		for i, j in enumerate(outcome.commands):
			truth = r["expected"][i] if "expected" in r else \
				("refused" if (case, i) in FIRST_REFUSALS else "worked")
			if j.verdict == Verdict.NAVIGATION or truth is None:
				continue
			rows.append((case, j.command, truth, j.verdict, j.note, said[i]))
	return rows


ROWS = commands()


def test_the_corpus_holds_refusals_and_working_commands():
	"""The guards below mean something: refusals and working commands of every kind."""
	assert sum(1 for row in ROWS if row[2] == "refused") >= 29
	assert sum(1 for row in ROWS if row[2] == "worked") >= 91


def test_a_refused_command_is_never_accepted():
	"""Never a false success: no refused command took effect, is in place, or removed."""
	assert [(c, cmd, v) for c, cmd, truth, v, _, _ in ROWS if truth == "refused" and v in ACCEPTED] == []


def test_every_refused_command_is_rejected():
	"""Each refusal on all three devices - typos, wrong mode, bad values, a missing
	licence, a removal of nothing, a FortiOS entry discarded when its block closed -
	is rejected, none left for review."""
	assert [(c, cmd, v) for c, cmd, truth, v, _, _ in ROWS
	        if truth == "refused" and v != Verdict.REJECTED] == []


def test_the_working_commands_rejected_are_exactly_the_known_ones():
	assert {(c, cmd) for c, cmd, truth, v, _, _ in ROWS
	        if truth == "worked" and v == Verdict.REJECTED} == KNOWN_FALSE_REJECTIONS


def test_the_commands_left_for_review_are_exactly_the_special_cases():
	"""Coverage: more review than this is a regression - the configuration
	proves the rest, or the device's reply rejects it."""
	assert {(c, cmd) for c, cmd, _, v, _, _ in ROWS if v == Verdict.REVIEW} == REVIEW


@pytest.mark.parametrize("case, command", [
	("R1/exec-do-show", "do show clock"), ("R1/exec-do-write", "do write memory"),
	("NXOS1/exec-do-show", "do show clock"), ("NXOS1/exec-show-no-do", "show clock"),
	("FGT1/exec-get-top", "get system status"), ("FGT1/exec-get-in-block", "get")])
def test_exec_commands_are_not_judged(case, command):
	assert [v for c, cmd, _, v, _, _ in ROWS if (c, cmd) == (case, command)] == [Verdict.EXEC]


def test_already_absent_never_carries_a_reply():
	"""A removal that found nothing to remove is certain only when the device
	said nothing - a mistyped one finds nothing either, and the device objects."""
	assert [(c, cmd) for c, cmd, _, _, note, said in ROWS if note == "already absent" and said] == []


@pytest.mark.parametrize("case, command, evidence, note", [
	# the configuration shown with its defaults, read in the same run
	("FGT1/defaults-back-to-default", "set admintimeout 5", "  set admintimeout 5", "shown with the device's defaults"),
	("FGT1/defaults-same-warning", "set admin-sport 443", "  set admin-sport 443", "shown with the device's defaults"),
	("NXOS1/defaults-noshut", "no shutdown", "  no shutdown", "shown with the device's defaults"),
	# the push removed its negation - IOS hides "ip domain lookup" even with defaults
	("R1/defaults-domain-lookup", "ip domain lookup", "- no ip domain lookup", "its negation was removed"),
	# a negation the device keeps as a line
	("R1/neg-kept-lines", "no ip redirects", "+ no ip redirects", ""),
	("R1/neg-kept-global", "no cdp run", "+ no cdp run", ""),
	# this NX-OS prints the default "ip domain-lookup": the removal took it away
	("NXOS1/neg-kept-global", "no ip domain-lookup", "- ip domain-lookup", ""),
])
def test_proven_by_defaults_negation_or_a_kept_negation(case, command, evidence, note):
	_, outcome, _ = judged(LAB / f"{case}.json.gz")
	j = next(j for j in outcome.commands if j.command == command)
	assert j.verdict in ACCEPTED and j.evidence == (evidence,) and j.note == note
