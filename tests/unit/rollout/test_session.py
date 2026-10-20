"""One device's conversation (src/rollout/session.py): what the device
replied to each command, told apart by shape from the echo and the prompt -
on the replies recorded from the lab (tests/unit/rollout/lab)."""
import gzip
import json
from pathlib import Path

from src.rollout.session import reply

LAB = Path(__file__).parent / "lab"


def recorded(case):
	with gzip.open(LAB / f"{case}.json.gz", "rt", encoding="utf-8") as f:
		return json.load(f)


def test_replies_are_kept_apart_from_echo_and_prompt():
	"""The reply is what the device said beyond the echo, the prompt and
	IOS's ^ pointer - for every platform, whatever its wording."""
	assert reply(recorded("R1/refused-ntp")["replies"][0], "ntp server 192.0.2.123", "R1") == (
		["% Initialization of NTP failed with status 4.", "%NTP: failed to initialize NTP process"], [])
	assert reply(recorded("R1/typo")["replies"][0], "foo bar", "R1") == (
		["% Invalid input detected at '^' marker."], [])
	assert reply(recorded("FGT1/typo")["replies"][0], "foo bar", "FGT1 ") == (["Unknown action 0"], [])
	assert reply(recorded("FGT1/named-entry")["replies"][1], "edit nr-test", "FGT1 ") == (
		["new entry 'nr-test' added"], [])
	assert reply(recorded("NXOS1/value-new")["replies"][0], "snmp-server location A", "NXOS1") == ([], [])
	assert reply(recorded("FGT1/value-unset")["replies"][1], "set alias nr-alias", "FGT1 ") == ([], [])


def test_time_stamped_lines_are_events_not_replies():
	"""A syslog line between commands is a device event, kept apart."""
	output = "ntp server 1.2.3.4\n*Oct 19 19:17:26.223: NTP Core (EMERGENCY): selftest failed\nR1(config)#"
	assert reply(output, "ntp server 1.2.3.4", "R1") == (
		[], ["*Oct 19 19:17:26.223: NTP Core (EMERGENCY): selftest failed"])
