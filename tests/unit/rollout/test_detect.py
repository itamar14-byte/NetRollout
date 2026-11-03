"""Platform detection (src/rollout/detect.py and each platform's Identity):
which platform an answer is, the probes and their order, pagers, shells,
the time limits, the hostname. Every recorded device output in
tests/unit/rollout/detect/ (Apache-2.0 projects, NOTICE there) must be
claimed by its own platform alone; the short excerpts here add the cases
no recorded output covers (vendor documentation and the research of
2026-11-03: docs/plans/platform-detect-and-discover.md)."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import netmiko
import pytest

from src.rollout import detect
from src.rollout.detect import DETECT_SECONDS, NEIGHBOURS, Detection, identify, identities, probes
from src.rollout.engine import Device
from src.rollout.platforms import PLATFORMS, Identity, hostname, text


ANSWERS = {
	"cisco_ios": ("show version",
	              "Cisco IOS Software, 3700 Software (C3725-ADVENTERPRISEK9-M), Version 12.4(15)T14, "
	              "RELEASE SOFTWARE (fc2)\nR1 uptime is 2 weeks"),
	"cisco_ios (12.0)": ("show version",
	                     "Cisco Internetwork Operating System Software\nIOS (tm) C2600 Software (C2600-I-M), "
	                     "Version 12.0(7)T"),
	"cisco_xe": ("show version",
	             "Cisco IOS XE Software, Version 16.09.04\nCisco IOS Software [Fuji], Catalyst L3 Switch "
	             "Software (CAT9K_IOSXE), Version 16.9.4, RELEASE SOFTWARE (fc2)"),
	"cisco_xe (3.x)": ("show version",
	                   "Cisco IOS Software, IOS-XE Software, Catalyst 4500 L3 Switch Software "
	                   "(cat4500e-UNIVERSALK9-M), Version 03.06.06.E RELEASE SOFTWARE (fc1)"),
	"cisco_xr": ("show version", "Cisco IOS XR Software, Version 7.1.1\nCopyright (c) 2013-2020 by Cisco Systems"),
	"cisco_nxos": ("show version", "Cisco Nexus Operating System (NX-OS) Software\nTAC support: http://www.cisco.com/tac"),
	"arista_eos": ("show version", "Arista vEOS-lab\nHardware version:\n\nSoftware image version: 4.27.0F"),
	"juniper_junos": ("show version", "Hostname: srx1\nModel: srx240h\nJUNOS Software Release [12.1X44-D45.2]"),
	"juniper_junos (21+)": ("show version", "Hostname: JS1\nModel: ex9214\nJunos: 24.4R1.9\nJUNOS OS Kernel 64-bit"),
	"aruba_aoscx": ("show version", "ArubaOS-CX\n(c) Copyright Hewlett Packard Enterprise\nVersion      : FL.10.11.1010"),
	"hp_procurve": ("show version",
	                "Image stamp:    /ws/swbuildm/rel_tacoma_qaoff/code/build/btm\n                WC.16.08.0001"),
	"fortinet": ("get system status", "Version: FortiGate-VM64 v7.2.4,build1396,230131 (GA.F)\nSerial-Number: FGVM01"),
	"paloalto_panos": ("show system info", "hostname: PA1\nmodel: PA-VM\nsw-version: 11.2.5"),
	"paloalto_panos (Panorama)": ("show system info", "hostname: pano1\nmodel: Panorama\nsw-version: 11.1.2"),
	"hp_comware": ("display version", "HPE Comware Software, Version 7.1.070, Release 3208P08"),
	"hp_comware (H3C)": ("display version", "H3C Comware Software, Version 7.1.064, Release 0427P22"),
	"checkpoint_gaia": ("show version all", "Product version Check Point Gaia R81.20\nOS build 631"),
	"cisco_asa": ("show version", "Cisco Adaptive Security Appliance Software Version 9.12(4)"),
	"cisco_ftd": ("show version",
	              "Cisco Firepower Threat Defense for VMware v6.6.0 (build 90)\n"
	              "Cisco Adaptive Security Appliance Software Version 9.14(1)"),
	"huawei": ("display version", "Huawei Versatile Routing Platform Software\nVRP (R) software, Version 8.180"),
	"linux": ("show version", "-bash: show: command not found"),
	# the edge cases (research 2026-11-03)
	"cisco_xe (16.4 Cat3850)": ("show version",
	                            "Cisco IOS Software, Catalyst L3 Switch Software (CAT3K_CAA-UNIVERSALK9-M), "
	                            "Version Denali 16.4.1\nCisco IOS-XE software, Copyright (c) 2005-2016 by cisco"),
	"cisco_ios (12.3 C2600)": ("show version", "IOS (tm) C2600 Software (C2600-I-M), Version 12.3(6)"),
	"juniper_junos (EX 13.2X51)": ("show version",
	                               "fpc0:\n----\nHostname: ex1\nModel: ex4300-48t\n"
	                               "JUNOS EX  Software Suite [13.2X51-D35.3]"),
	"juniper_junos (Evolved)": ("show version", "Hostname: ptx1\nModel: ptx10008\nJunos: 22.4R1.11-EVO"),
	"paloalto_panos (M-100)": ("show system info", "hostname: M-100\nmodel: M-100\nfamily: m"),
	"hp_comware (CMW5)": ("display version", "H3C Comware Platform Software\nComware Software, Version 5.20"),
	"hp_procurve (wrapped stamp)": ("show version",
	                                "Image stamp:\n  /ws/swbuildm/rel_tacoma_qaoff/code/build/btm\n  WB.15.15.0006"),
	"cisco_aci": ("show version", "Cisco Nexus Operating System (NX-OS) Software\n"
	                              "  kickstart image file is: /bootflash/aci-n9000-dk9.14.0.1h.bin"),
	"cisco_mds": ("show version", "Cisco Nexus Operating System (NX-OS) Software\nHardware\n"
	                              "  cisco MDS 9148S 16G 48 FC (1 Slot) Chassis"),
}

RECORDED = sorted(Path(__file__).parent.joinpath("detect").glob("*/*"))


def claims(answer, probe):
	""":returns: the names whose identity claims the answer to the probe"""
	return sorted(name for name, identity in identities().items()
	              if identity.probe == probe and identity.evidence(answer))


def test_every_platform_recognises_itself():
	"""Every supported platform has an identity - detection can name each."""
	assert all(platform.identity is not None for platform in PLATFORMS.values())


@pytest.mark.parametrize("case", sorted(ANSWERS))
def test_each_answer_is_claimed_by_its_platform_alone(case):
	"""Each platform's (or neighbour's) answer is claimed by it and by no
	other identity - an IOS-XE never by IOS, an FTD never by ASA."""
	probe, answer = ANSWERS[case]
	assert claims(answer, probe) == [case.split(" ")[0]]


@pytest.mark.parametrize("path", RECORDED, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_each_recorded_output_is_claimed_by_its_platform_alone(path):
	"""A real device's answer (its folder names what it is; none: nothing
	NetRollout names) is claimed by that identity and no other."""
	answer = path.read_text(encoding="utf-8", errors="replace")
	claimed = sorted(name for name, identity in identities().items() if identity.evidence(answer))
	assert claimed == ([] if path.parent.name == "none" else [path.parent.name])


def test_every_identity_is_tested_against_an_answer():
	"""Each platform and neighbour has a recorded output or an excerpt here."""
	tested = {p.parent.name for p in RECORDED} | {case.split(" ")[0] for case in ANSWERS}
	assert set(identities()) <= tested


def test_a_vendor_word_in_a_hostname_claims_nothing_else():
	"""Other vendors' names in a Cisco's answer (its hostname, uptime line)
	don't make it theirs: still IOS alone."""
	answer = ("Cisco IOS Software, C2900 Software, Version 15.4(3)M3\n"
	          "arista-nokia-aruba-edge uptime is 2 weeks")
	assert claims(answer, "show version") == ["cisco_ios"]


def test_the_evidence_is_the_line_recognised_by():
	"""The evidence is the answer's whole line holding the mark, stripped."""
	identity = PLATFORMS["paloalto_panos"].identity
	assert identity.evidence("hostname: PA1\n  model: PA-VM\nsw-version: 11.2.5") == "model: PA-VM"


class FakeDevice:
	"""A signed-in session: each command's answer, then the prompt; an answer
	ending in a pager waits for its key, then the prompt."""
	RETURN = "\n"

	def __init__(self, prompt, answers=None, pager=None):
		self.prompt = prompt
		self.answers = answers or {}
		self.pager = pager
		self.sent = []
		self.pending = ""

	def find_prompt(self):
		return self.prompt

	def write_channel(self, data):
		self.sent.append(data)
		if data.endswith(self.RETURN):
			command = data[:-1]
			answer = self.answers.get(command, "% Invalid input detected at '^' marker.")
			paged = self.pager is not None and answer.endswith(self.pager)
			self.pending += f"{command}\n{answer}" + ("" if paged else f"\n{self.prompt}")
		else:
			self.pending += f"\n{self.prompt}"

	def read_channel(self):
		out, self.pending = self.pending, ""
		return out

	def commands(self):
		return [s[:-1] for s in self.sent if s.endswith(self.RETURN)]


def test_identify_names_the_platform_its_hostname_and_the_evidence():
	"""An IOS-XE answer: cisco_xe, the hostname from the prompt, the line
	it was recognised by - after one probe."""
	device = FakeDevice("CORE-SW1#", {"show version": ANSWERS["cisco_xe"][1]})
	assert identify(device) == Detection(platform="cisco_xe", detected="cisco_xe", hostname="CORE-SW1",
	                                     evidence="Cisco IOS XE Software, Version 16.09.04")
	assert device.commands() == ["show version"]


def test_identify_goes_on_until_a_probe_is_claimed():
	"""Gaia in clish: show version and the others refused, show version all
	claimed - every probe before it asked once."""
	device = FakeDevice("CP1:0>", {"show version all": ANSWERS["checkpoint_gaia"][1]})
	found = identify(device)
	assert (found.platform, found.hostname) == ("checkpoint_gaia", "CP1")
	assert device.commands()[-1] == "show version all"
	assert len(device.commands()) == len(set(device.commands()))


def test_a_neighbour_is_named_but_isnt_a_platform():
	"""An ASA: detected cisco_asa, no platform - NetRollout doesn't support it."""
	found = identify(FakeDevice("asa1#", {"show version": ANSWERS["cisco_asa"][1]}))
	assert (found.platform, found.detected) == (None, "cisco_asa")


def test_nothing_claimed_is_unknown():
	"""No answer claimed by any identity: nothing detected, no guess."""
	assert identify(FakeDevice("box>")) == Detection()


def test_two_claims_are_unknown(monkeypatch):
	"""An answer two identities claim is never decided: unknown, with both names."""
	monkeypatch.setitem(NEIGHBOURS, "lookalike", Identity("show version", text("Cisco IOS Software")))
	found = identify(FakeDevice("R1#", {"show version": ANSWERS["cisco_ios"][1]}))
	assert (found.platform, found.detected) == (None, None)
	assert "cisco_ios" in found.problem and "lookalike" in found.problem


def test_a_login_disclaimer_is_never_accepted():
	"""A FortiGate's post-login disclaimer: nothing sent, the reason said."""
	device = FakeDevice("(Press 'a' to accept):")
	assert identify(device).problem == "The device shows a login disclaimer"
	assert device.sent == []


def test_no_prompt_is_unknown():
	"""A session that never shows a prompt: unknown, the reason said."""
	device = FakeDevice("R1#")
	device.find_prompt = MagicMock(side_effect=ValueError("Unable to find prompt"))
	assert identify(device).problem == "The device showed no prompt"


@pytest.mark.parametrize("prompt, first", [
	("<HPE>", "display version"),
	("FGT1 #", "get system status"),
	("FGT1 (root) #", "get system status"),
	("admin@PA-VM>", "show version"),
	("[Expert@CP1:0]#", 'clish -c "show version all"'),
	("root@srx:~ #", 'cli -c "show version"'),
	("root@srx%", 'cli -c "show version"'),
	("R1#", "show version"),
])
def test_the_prompt_decides_what_is_asked_first(prompt, first):
	"""The prompt's shape puts its platform's probe first (a shell's through
	the shell); every probe still comes, once."""
	order = probes(prompt, identities())
	assert order[0][0] == first
	assert len(order) == len(set(order))
	assert {probe for _, probe in order} == {i.probe for i in identities().values()}


def test_a_user_at_prompt_asks_panos_right_after_junos():
	"""user@host> is Junos or PAN-OS: show version, then show system info."""
	assert [command for command, _ in probes("admin@PA-VM>", identities())[:2]] == [
		"show version", "show system info"]


def test_a_fortigate_is_detected_with_one_command():
	"""FGT1 #: get system status first - the only command sent."""
	device = FakeDevice("FGT1 #", {"get system status": ANSWERS["fortinet"][1]})
	assert identify(device).platform == "fortinet"
	assert device.commands() == ["get system status"]


@pytest.mark.parametrize("prompt, command, case", [
	("[Expert@CP1:0]#", 'clish -c "show version all"', "checkpoint_gaia"),
	("root@srx:~ #", 'cli -c "show version"', "juniper_junos"),
])
def test_a_shell_is_asked_through_its_cli(prompt, command, case):
	"""Gaia's expert shell and Junos root's Unix shell: the probe through
	clish / cli, the shell left as it was - claimed by the platform."""
	device = FakeDevice(prompt, {command: ANSWERS[case][1]})
	assert identify(device).platform == case
	assert device.commands() == [command]


@pytest.mark.parametrize("pager, key", [
	(" --More-- ", "q"),
	("---(more 42%)---", "q"),
	("-- MORE --, next page: Space, next line: Enter, quit: Control-C", "\x03"),
])
def test_a_pager_is_left_with_its_key(pager, key):
	"""An answer that pages: the pager's key sent, the first page claimed -
	the next command never lands in the pager."""
	device = FakeDevice("R1#", {"show version": ANSWERS["cisco_ios"][1] + "\n" + pager}, pager=pager)
	assert identify(device).platform == "cisco_ios"
	assert key in device.sent


def test_aoscx_pager_is_left_with_q():
	"""AOS-CX names its quit key "q" in the same pager line as ProCurve's."""
	pager = "-- MORE --, next page: Space, next line: Enter, quit: q"
	device = FakeDevice("6300#", {"show version": ANSWERS["aruba_aoscx"][1] + "\n" + pager}, pager=pager)
	assert identify(device).platform == "aruba_aoscx"
	assert "q" in device.sent and "\x03" not in device.sent


def test_procurve_any_key_splash_is_answered():
	"""ProCurve's "Press any key to continue" after login: answered, then
	the real prompt read."""
	device = FakeDevice("HP-2920#", {"show version": ANSWERS["hp_procurve"][1]})
	prompts = iter(["Press any key to continue", "HP-2920#"])
	device.find_prompt = lambda: next(prompts)
	found = identify(device)
	assert (found.platform, found.hostname) == ("hp_procurve", "HP-2920")
	assert device.sent[0] == "\n"


@pytest.mark.parametrize("case, prompt, name", [
	("cisco_ios", "custom-prompt#", "R1"),
	("juniper_junos (EX 13.2X51)", "netops@whatever>", "ex1"),
	("paloalto_panos", "admin@x>", "PA1"),
])
def test_the_hostname_comes_from_the_answer_first(case, prompt, name):
	"""Where the answer says the name (IOS "R1 uptime is", Junos "Hostname:",
	PAN-OS "hostname:"), it beats the prompt - which can be set to anything."""
	probe, answer = ANSWERS[case]
	assert identify(FakeDevice(prompt, {probe: answer})).hostname == name


def test_carriage_returns_and_backspaces_are_read_through():
	"""NX-OS's doubled carriage returns and a pager's backspaces don't hide
	the line or the name."""
	answer = "Cisco Nexus Operating System (NX-OS) Software\r\r\nDevice\x08 name: N9K1\r\r\n"
	found = identify(FakeDevice("N9K1#", {"show version": answer}))
	assert (found.platform, found.hostname, found.evidence) == (
		"cisco_nxos", "N9K1", "Cisco Nexus Operating System (NX-OS) Software")


def test_detection_stops_at_its_time_limit():
	"""A device that never answers with its prompt: every wait counted,
	detection gives up within DETECT_SECONDS - unknown, no hang."""
	now = [0.0]
	device = FakeDevice("R1#")
	device.read_channel = lambda: "still working"
	ticks = []

	def clock():
		now[0] += 0.5
		ticks.append(now[0])
		return now[0]
	found = identify(device, clock=clock, pause=lambda s: None)
	assert found == Detection()
	assert ticks[-1] <= DETECT_SECONDS + 1


@pytest.mark.parametrize("platform, prompt, name", [
	("cisco_ios", "R1#", "R1"), ("cisco_ios", "R1>", "R1"), ("cisco_ios", "switch(config)#", "switch"),
	("cisco_xr", "RP/0/RP0/CPU0:XR1#", "XR1"), ("cisco_nxos", "NXOS1#", "NXOS1"),
	("hp_procurve", "HP-2920#", "HP-2920"), ("aruba_aoscx", "6300-core#", "6300-core"),
	("juniper_junos", "netrollout@JS1>", "JS1"), ("juniper_junos", "root@srx:~ #", "srx"),
	("juniper_junos", "root@srx%", "srx"),
	("paloalto_panos", "netrollout@PA1>", "PA1"), ("paloalto_panos", "admin@PA-VM(active)>", "PA-VM"),
	("fortinet", "FGT1 #", "FGT1"), ("fortinet", "FGT01 (root) #", "FGT01"), ("fortinet", "FGT01 $", "FGT01"),
	("hp_comware", "<HPE>", "HPE"), ("hp_comware", "[HPE]", "HPE"),
	("checkpoint_gaia", "CP1:0>", "CP1"), ("checkpoint_gaia", "CP1>", "CP1"),
	("checkpoint_gaia", "[Expert@CP1:0]#", "CP1"),
])
def test_the_hostname_from_each_platforms_prompt(platform, prompt, name):
	"""Each platform's prompt gives the device's name (the lab's prompts
	among them: R1, XR1, NXOS1, JS1, PA1, FGT1, CP1)."""
	assert hostname(PLATFORMS[platform].prompt_name, prompt) == name


def test_an_unreadable_prompt_gives_no_hostname():
	"""A prompt of another shape: no name rather than a wrong one."""
	assert hostname(PLATFORMS["hp_comware"].prompt_name, "R1#") is None


def device(**kwargs):
	return Device(ip="10.0.0.9", label="", username="netops", password="pw", device_type="",
	              secret="en", port=2222, **kwargs)


def test_detect_signs_in_with_the_generic_driver_and_disconnects():
	"""detect(): the rollout's login (address, port, credentials) with
	Netmiko's generic driver and no command verification; disconnected after."""
	conn = FakeDevice("R1#", {"show version": ANSWERS["cisco_ios"][1]})
	conn.disconnect = MagicMock()
	with patch("netmiko.ConnectHandler", return_value=conn) as connect:
		assert detect.detect(device()).platform == "cisco_ios"
	connect.assert_called_once_with(ip="10.0.0.9", username="netops", password="pw",
	                                device_type="terminal_server", port=2222, secret="en",
	                                global_cmd_verify=False)
	conn.disconnect.assert_called_once_with()


def test_detect_disconnects_when_identifying_fails():
	"""An error while probing still closes the session."""
	conn = MagicMock()
	conn.find_prompt.side_effect = OSError("socket closed")
	with patch("netmiko.ConnectHandler", return_value=conn), pytest.raises(OSError):
		detect.detect(device())
	conn.disconnect.assert_called_once_with()


@pytest.mark.parametrize("error", [netmiko.NetmikoAuthenticationException("no"),
                                   netmiko.NetmikoTimeoutException("slow")])
def test_detect_passes_a_sign_in_failure_through(error):
	"""Authentication failed and timed out reach the caller as themselves."""
	with patch("netmiko.ConnectHandler", side_effect=error), pytest.raises(type(error)):
		detect.detect(device())


def test_detect_retries_a_session_setup_timeout_once():
	"""A ReadTimeout while the session is set up (a device message redrew
	the prompt): signed in again once, as a rollout does."""
	conn = FakeDevice("R1#", {"show version": ANSWERS["cisco_ios"][1]})
	conn.disconnect = MagicMock()
	with patch("netmiko.ConnectHandler",
	           side_effect=[netmiko.exceptions.ReadTimeout("redrawn"), conn]) as connect:
		assert detect.detect(device(dns_suffix="")).platform == "cisco_ios"
	assert connect.call_count == 2
