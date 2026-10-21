"""Stage 4b: every supported platform - how the push finishes, how a refused
command is recognised, and the verify verdicts against the config as each
platform prints it (fixtures approximate real output; the EVE-NG round is
the real-device check)."""
import threading
import uuid
from unittest.mock import MagicMock, call, patch

import netmiko
import pytest

from src.rollout import inputs
from src.rollout.engine import Device, RolloutEngine, RolloutOptions
from src.rollout.evidence import Verdict, first_word, judge
from src.rollout.log import RolloutLogger
from src.rollout.platforms import (
	COMMIT_TIMEOUT, EVENTS, PLATFORMS, Commit, CommitPending, NoFinish, RunCommand, SaveConfig, fortios_blocks,
	no_defaults)
from src.rollout.session import NetmikoSession, PushResult, RunReport, reply


T, P, N, V = Verdict.TOOK_EFFECT, Verdict.IN_PLACE, Verdict.NAVIGATION, Verdict.REVIEW


def test_every_supported_platform_has_a_row():
	"""PLATFORMS has a row for exactly the supported platforms, no more, no fewer."""
	assert set(PLATFORMS) == set(inputs.SUPPORTED_PLATFORMS)


# ── Each platform's config layout, judged ────────────────────────────────────
# The configs as each platform prints them (kept from the verify tests the
# judge replaced): a change written into one, the way the platform would,
# must be found in its section and attributed to the command - the 9
# platforms the EVE lab doesn't run get their layout covered here.

CONFIGS = {
	'cisco_ios': 'Building configuration...\n\nCurrent configuration : 1980 bytes\n!\nversion 17.3\nhostname r1\n!\nno ip domain lookup\n!\ninterface GigabitEthernet1\n description uplink-to-core\n ip address 10.0.0.1 255.255.255.0\n negotiation auto\n!\ninterface GigabitEthernet2\n no ip address\n shutdown\n!\nrouter bgp 65000\n bgp log-neighbor-changes\n neighbor 10.0.0.2 remote-as 65001\n !\n address-family ipv4\n  network 10.0.0.0 mask 255.255.255.0\n  neighbor 10.0.0.2 activate\n exit-address-family\n!\nip route 0.0.0.0 0.0.0.0 10.0.0.254\nntp server 1.1.1.1\nbanner motd ^C\nAuthorized access only\n^C\n!\nend\n',
	'cisco_xe': 'Building configuration...\n\nCurrent configuration : 1980 bytes\n!\nversion 17.3\nhostname r1\n!\nno ip domain lookup\n!\ninterface GigabitEthernet1\n description uplink-to-core\n ip address 10.0.0.1 255.255.255.0\n negotiation auto\n!\ninterface GigabitEthernet2\n no ip address\n shutdown\n!\nrouter bgp 65000\n bgp log-neighbor-changes\n neighbor 10.0.0.2 remote-as 65001\n !\n address-family ipv4\n  network 10.0.0.0 mask 255.255.255.0\n  neighbor 10.0.0.2 activate\n exit-address-family\n!\nip route 0.0.0.0 0.0.0.0 10.0.0.254\nntp server 1.1.1.1\nbanner motd ^C\nAuthorized access only\n^C\n!\nend\n',
	'cisco_nxos': '!Command: show running-config\n!Running configuration last done at: Thu Oct  1 10:00:00 2026\n\nversion 9.3(8) Bios:version\nhostname n1\nfeature ospf\nfeature interface-vlan\n\nvlan 1,10\nvlan 10\n  name users\n\ninterface Vlan10\n  no shutdown\n  ip address 10.10.0.1/24\n\ninterface Ethernet1/1\n  description to-n2\n  switchport access vlan 10\n\ninterface Ethernet1/2\n  shutdown\n\nrouter ospf 1\n  router-id 1.1.1.1\n',
	'cisco_xr': 'Building configuration...\n!! IOS XR Configuration 7.3.2\n!! Last configuration change at Thu Oct  1 10:00:00 2026 by admin\n!\nhostname x1\ninterface GigabitEthernet0/0/0/0\n description core\n ipv4 address 10.1.1.1 255.255.255.252\n!\ninterface GigabitEthernet0/0/0/1\n shutdown\n!\nrouter ospf 1\n area 0\n  interface GigabitEthernet0/0/0/0\n   cost 10\n  !\n !\n!\nend\n',
	'arista_eos': '! Command: show running-config\n! device: e1 (vEOS-lab, EOS-4.27.0F)\n!\nhostname e1\n!\ninterface Ethernet1\n   description srv\n   switchport access vlan 20\n!\nip routing\n!\nrouter bgp 65001\n   neighbor 10.0.0.2 remote-as 65002\n!\nend\n',
	'aruba_aoscx': 'Current configuration:\n!\n!Version ArubaOS-CX GL.10.08.1010\n!export-password: default\nhostname a1\nvlan 1,10\nvlan 10\n    name users\ninterface 1/1/1\n    no shutdown\n    description access\n    vlan access 10\n',
	'hp_procurve': 'Running configuration:\n\n; J9773A Configuration Editor; Created on release #YA.16.10.0010\n; Ver #14:01.4f.f8.1d.9b.3f.bf.bb.ef.7c.59.fc.6b.fb.9f.fc.ff.ff.37.ef:05\n\nhostname "p1"\nmodule 1 type j9773a\nvlan 1\n   name "DEFAULT_VLAN"\n   untagged 1-23\n   ip address dhcp-bootp\n   exit\nvlan 10\n   name "users"\n   tagged 24\n   exit\n',
	'hp_comware': '#\n version 7.1.064, Release 0427P22\n#\n sysname c1\n#\nvlan 10\n name users\n#\ninterface GigabitEthernet1/0/1\n port link-mode bridge\n description to-core\n port access vlan 10\n#\ninterface GigabitEthernet1/0/2\n port link-mode bridge\n shutdown\n#\nreturn\n',
	'juniper_junos': 'set version 21.4R1.12\nset system host-name j1\nset system services ssh\nset interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/24\nset protocols ospf area 0.0.0.0 interface ge-0/0/0.0\nset routing-options static route 0.0.0.0/0 next-hop 10.0.0.254\n',
	'paloalto_panos': 'set deviceconfig system hostname fw1\nset network interface ethernet ethernet1/1 layer3 ip 10.0.0.1/24\nset zone trust network layer3 ethernet1/1\n',
	'checkpoint_gaia': '#\n# Configuration of gw-1\n# Language version: 14.1v1\n#\n# Exported by admin on Thu Oct  1 10:00:00 2026\n#\nset hostname gw-1\nset interface eth1 ipv4-address 10.0.0.1 mask-length 24\nset interface eth1 state on\nadd user ops uid 2001 homedir /home/ops\nset static-route default nexthop gateway address 10.0.0.254 on\n',
	'fortinet': '#config-version=FGVM64-7.2.4-FW-build1396-230131:opmode=0:vdom=0:user=admin\n#conf_file_ver=12345\n#buildno=1396\n#global_vdom=1\nconfig system global\n    set hostname "fw1"\n    set timezone 28\nend\nconfig system interface\n    edit "port1"\n        set vdom "root"\n        set ip 10.0.0.1 255.255.255.0\n        set allowaccess ping https ssh\n        config ipv6\n            set ip6-address 2001:db8::1/64\n        end\n    next\n    edit "port2"\n        set vdom "root"\n        set ip 10.1.0.1 255.255.255.0\n    next\nend\nconfig firewall address\n    edit "srv1"\n        set subnet 10.2.0.10 255.255.255.255\n    next\nend\n',
}


# (platform, the row the new line goes under - None: appended (flat configs),
#  the new line as the device prints it, the commands typed, their verdicts)
INSERTS = [
	("cisco_ios", "interface GigabitEthernet2", " description new-link",
	 ["interface GigabitEthernet2", "description new-link", "exit"], [P, T, N]),
	("cisco_xe", "router bgp 65000", " neighbor 10.0.0.9 remote-as 65009",
	 ["router bgp 65000", "neighbor 10.0.0.9 remote-as 65009"], [P, T]),
	("cisco_nxos", "interface Ethernet1/2", "  description spare",
	 ["int eth1/2", "description spare"], [P, T]),
	("cisco_xr", "  interface GigabitEthernet0/0/0/0", "   network point-to-point",
	 ["router ospf 1", "area 0", "interface GigabitEthernet0/0/0/0", "network point-to-point"], [P, P, P, T]),
	("arista_eos", "router bgp 65001", "   neighbor 10.0.0.3 remote-as 65003",
	 ["router bgp 65001", "neighbor 10.0.0.3 remote-as 65003"], [P, T]),
	("aruba_aoscx", "interface 1/1/1", "    mtu 9198", ["interface 1/1/1", "mtu 9198"], [P, T]),
	("hp_procurve", "vlan 10", "   untagged 5", ["vlan 10", "untagged 5", "exit"], [P, T, N]),
	("hp_comware", "interface GigabitEthernet1/0/2", " description spare",
	 ["interface GigabitEthernet1/0/2", "description spare", "quit"], [P, T, N]),
	("juniper_junos", None, "set system ntp server 10.0.0.5",
	 ["edit system", "set ntp server 10.0.0.5", "top"], [N, T, N]),
	("paloalto_panos", None, "set deviceconfig system ntp-servers primary-ntp-server ntp-server-address 10.0.0.5",
	 ["set deviceconfig system ntp-servers primary-ntp-server ntp-server-address 10.0.0.5"], [T]),
	("checkpoint_gaia", None, "set ntp server primary 10.0.0.5 version 4",
	 ["set ntp server primary 10.0.0.5 version 4"], [T]),
	("fortinet", "config system global", "    set admintimeout 30",
	 ["config system global", "set admintimeout 30", "end"], [P, T, N]),
]


def with_line(config, under, line):
	""":returns: the config with `line` right below the row `under` (exactly that
	 row, indentation included); appended when under is None"""
	if under is None:
		return config.rstrip("\n") + "\n" + line + "\n"
	rows = config.splitlines()
	at = rows.index(under) if under in rows else rows.index(under.strip())
	return "\n".join(rows[:at + 1] + [line] + rows[at + 1:]) + "\n"


@pytest.mark.parametrize("device_type, under, line, commands, verdicts", INSERTS,
                         ids=[i[0] for i in INSERTS])
def test_a_change_is_found_in_its_section_on_each_platform(device_type, under, line, commands, verdicts):
	"""On each platform's own layout (sections, FortiOS blocks, Comware's '#',
	ProCurve's 'exit', flat set lines with Junos' edit path), a new line is the
	command that made it, and the section it went into is already in place."""
	platform = PLATFORMS[device_type]
	before = platform.config(CONFIGS[device_type])
	after = platform.config(with_line(CONFIGS[device_type], under, line))
	outcome = judge(commands, before, after, flat=platform.flat, blocks=platform.close_blocks)
	assert [j.verdict for j in outcome.commands] == verdicts
	assert outcome.other_changes == ()


@pytest.mark.parametrize("device_type", list(CONFIGS))
def test_nothing_changed_is_never_a_success(device_type):
	"""The same config before and after: a command that isn't already there
	exactly is review, never took effect - on every platform."""
	platform = PLATFORMS[device_type]
	config = platform.config(CONFIGS[device_type])
	outcome = judge(["snmp-server location nowhere-at-all"], config, config,
	                flat=platform.flat, blocks=platform.close_blocks)
	assert [j.verdict for j in outcome.commands] == [V]


def test_ios_framing_is_left_out():
	"""IOS prints "Building configuration..." and "Current configuration : N
	bytes" above the config; the count changes with every push - framing, not
	configuration."""
	config = PLATFORMS["cisco_ios"].config(CONFIGS["cisco_ios"])
	assert "Current configuration" not in config and "Building configuration" not in config
	assert "hostname r1" in config


@pytest.mark.parametrize("command, word", [
	('""', ""), ('" "', ""), ("", ""), ("   ", ""),
	('  "Exit" now', "exit"), ("Hostname R1", "hostname"),
])
def test_first_word_of_a_command(command, word):
	"""A command's first word as compared; a line that normalizes to nothing
	(blank, or only quotes) has the empty word instead of raising."""
	assert first_word(command) == word


# ── What a device replies, in each vendor's words ────────────────────────────
# (kept from the rejection tests the judge replaced: never judged now, but
# every one of them must reach the log and the device's transcript)

@pytest.mark.parametrize("command, output", [
	("ip adress 1.1.1.1", "r1(config)#ip adress 1.1.1.1\n                   ^\n% Invalid input detected at '^' marker.\nr1(config)#"),
	("feature bgpp", "% Invalid command at '^' marker."),             # NX-OS
	("vlan acess 10", "% Invalid input"),                             # EOS / Aruba
	("router ospf", "% Incomplete command."),
	("sh", "% Ambiguous command:  \"sh\""),
	("tagged x", "Invalid input: x"),                                 # ProCurve
	("sysnam c1", "% Unrecognized command found at '^' position."),   # Comware
	("set system hostnam j1", "syntax error.\n[edit]"),               # Junos
	("set foo", "error: configuration check-out failed"),
	("set deviceconfig foo", "Invalid syntax."),                      # PAN-OS
	("sett zone", "Unknown command: sett"),
	("set interfce eth1", "CLINFR0329  Invalid command:'set interfce eth1'."),  # Gaia
	("set ipp 1.1.1.1", "command parse error before 'ipp'\nCommand fail. Return code -61"),  # FortiOS
	("set srcintf LAN", "node_check_object fail! for name LAN\nvalue parse error before 'LAN'"),
	("set member web", "entry not found in datasource"),
	('set alias "LAN', "token line: Unmatched double quote."),
	("set routing-options autonomous-system 0", "invalid autonomous system value at '0' not in range 1 to 65535"),
	("set protocols bgp group", "missing argument."),
	("set rulebase security rules r1 to x", "Validation Error: rulebase -> security -> rules -> r1 -> to 'x' is not a valid reference"),
	("router bgp", "% Incomplete command"),                           # EOS
	("ip ospf", "% Error: ...\n"),
	("set static-route x", "CLINFR0349  Incomplete command."),             # Gaia
	("set ospf area x", "RTGRTG0019  OSPF: Area value must be ..."),
	("add user x", "-bash: add: command not found"),
	("port access vlan x", "% Wrong parameter found at '^' position."),   # Comware
	("vlan 10 20 30 40", "% Too many parameters found at '^' position."),
	("spanning-tree priority", "Incomplete input: priority"),            # ProCurve
	("interface", "% Command incomplete."),                              # Aruba CX
])
def test_every_vendors_error_reaches_the_reply(command, output):
	"""Each vendor's error (IOS, NX-OS, EOS, Aruba, ProCurve, Comware, Junos,
	PAN-OS, Gaia, FortiOS) is kept as the device's reply - whatever its words."""
	said, _ = reply(output, command, "r1")
	assert said and all(line in output for line in said)


@pytest.mark.parametrize("command, output, prompt", [
	("description unknown-host", "r1(config-if)#description unknown-host\nr1(config-if)#", "r1"),
	("set comments invalid-input-filter", "fw1 (port1) # set comments invalid-input-filter\nfw1 (port1) #", "fw1"),
	("hostname r2", "r2(config)#", "r2"),
])
def test_an_accepted_commands_echo_and_prompt_are_no_reply(command, output, prompt):
	"""A command echoed back with error-like words in its own text, or a bare
	new prompt (a new hostname's), is no reply."""
	said, _ = reply(output, command, prompt)
	assert said == []


# ── How the push finishes, per platform ──────────────────────────────────────

def push(device_type, conn, commands=("hostname x",), logger=None):
	"""The engine's push of `commands` to one device over the mocked `conn`: its PushResult."""
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type=device_type, secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], list(commands))
	with patch("netmiko.ConnectHandler", return_value=conn):
		_, results = engine._push_config(
			threading.Event(),
			logger or RolloutLogger(webapp=False, verbose=False))
	return results[0]


def connection(prompt="r1#", vdoms=False, cfg_save="automatic"):
	"""A mocked Netmiko connection: accepts every command, shows `prompt`, FortiOS's
	VDOM flag and cfg-save mode."""
	conn = MagicMock()
	conn.send_config_set.side_effect = lambda commands, **_: f"{commands[0]}\n{prompt}"
	conn.base_prompt = prompt.rstrip("#> ")
	conn.find_prompt.return_value = prompt
	conn._vdoms = vdoms                        # Netmiko's FortiOS VDOM flag
	conn.send_command_timing.side_effect = lambda cmd: (
		f"cfg-save            : {cfg_save}" if "cfg-save" in cmd else "")
	return conn


def finish_calls(conn):
	"""The calls that finish the push (commit, save, leaving config mode, sends), minus
	the pushed command itself."""
	return [c for c in conn.method_calls
	        if c[0] in ("commit", "exit_config_mode", "save_config",
	                    "send_command", "send_config_set", "send_command_timing")
	        and c != call.send_config_set(["hostname x"], enter_config_mode=False,
	                                    exit_config_mode=False)]


@pytest.mark.parametrize("device_type", [t for t, p in PLATFORMS.items()
                                         if isinstance(p.finish, SaveConfig)])
def test_save_platforms_leave_config_mode_then_save(device_type):
	"""Every "save" platform leaves config mode, then saves (Aruba CX sends `end` first);
	the push is applied with nothing rejected."""
	conn = connection()
	assert push(device_type, conn).applied
	first = [call.send_command_timing("end")] \
		if device_type == "aruba_aoscx" else []
	assert finish_calls(conn) == [*first, call.exit_config_mode(),
	                              call.save_config()]


def test_aruba_cx_leaves_sub_contexts_before_saving():
	"""From "(config-if)#", Aruba CX sends a command (`end`) before the save. Netmiko's
	AOS-CX driver only recognises "(config)#": its exit would do nothing and the save
	would run inside the interface."""
	conn = connection(prompt="a1(config-if)#")
	assert push("aruba_aoscx", conn).applied
	names = [c[0] for c in conn.method_calls]
	assert names.index("send_command_timing") < names.index("save_config")


@pytest.mark.parametrize("device_type", ["juniper_junos", "paloalto_panos",
                                         "cisco_xr"])
def test_commit_platforms_commit_before_leaving_config_mode(device_type):
	"""Junos, PAN-OS and IOS XR commit (with the commit timeout), then leave config mode."""
	conn = connection()
	assert push(device_type, conn).applied
	assert finish_calls(conn) == [call.commit(read_timeout=COMMIT_TIMEOUT), call.exit_config_mode()]


def test_junos_failed_commit_rolls_back_and_reports_not_applied():
	"""A failed Junos commit is followed by `rollback 0`, `show | compare` (what is
	left in the candidate) and leaving config mode; the push is reported not applied."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed: error: x")
	result = push("juniper_junos", conn)
	assert not result.applied
	assert finish_calls(conn) == [
		call.commit(read_timeout=COMMIT_TIMEOUT),
		call.send_config_set(["rollback 0"], exit_config_mode=False),
		call.send_config_set(["show | compare"], exit_config_mode=False),
		call.exit_config_mode()]


def test_a_discard_that_leaves_nothing_pending_needs_no_person():
	"""After a failed Junos commit, `show | compare` printing nothing but its echo,
	the `[edit]` banner and the prompt means nothing is left in the candidate: no
	ACTION NEEDED, and the log says so with the discard's reply."""
	conn = connection(prompt="u@j1#")
	conn.commit.side_effect = ValueError("Commit failed")
	conn.send_config_set.side_effect = lambda cmds, **kw: (
		"show | compare\n\n[edit]\nu@j1# " if cmds == ["show | compare"]
		else "load complete\n\n[edit]\nu@j1# " if cmds == ["rollback 0"] else "ok")
	logger = fresh_logger()
	assert not push("juniper_junos", conn, logger=logger).applied
	log = log_of(logger)
	assert "ACTION NEEDED" not in log
	assert "'rollback 0' (the device replied: load complete); 'show | compare' shows " \
	       "nothing left in the candidate" in log


def test_a_compare_that_cant_be_sent_asks_a_person():
	"""When the compare itself fails after the discard, nothing is claimed: ACTION
	NEEDED, check that the candidate configuration is empty."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")

	def answer(cmds, **kw):
		if cmds == ["show | compare"]:
			raise OSError("socket closed")
		return f"{cmds[0]}\nr1#"
	conn.send_config_set.side_effect = answer
	logger = fresh_logger()
	assert not push("juniper_junos", conn, logger=logger).applied
	assert "check that the candidate configuration is empty" in log_of(logger)


def fresh_logger():
	"""A rollout logger with its own file: a logger without a job id names it by the second."""
	return RolloutLogger(webapp=False, verbose=False, job_id=str(uuid.uuid4()))


def log_of(logger):
	with open(logger.logfile, encoding="utf-8") as f:   # the rollout log
		return f.read()


def test_panos_failed_commit_reverts_the_candidate():
	"""A failed PAN-OS commit sends `revert config`; when that works the log doesn't say
	the changes may still be in the candidate."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	logger = fresh_logger()
	assert not push("paloalto_panos", conn, logger=logger).applied
	assert call.send_config_set(["revert config"], exit_config_mode=False) \
	       in conn.method_calls
	assert "still be in the candidate" not in log_of(logger)


def test_xr_failed_commit_needs_no_discard_and_no_warning():
	"""A failed IOS XR commit sends no discard command (XR drops a failed commit by
	itself), leaves config mode, reports not applied - and the log doesn't ask to discard
	the changes on the device (unlike Junos / PAN-OS when their discard fails)."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	logger = fresh_logger()
	assert not push("cisco_xr", conn, logger=logger).applied
	assert finish_calls(conn) == [call.commit(read_timeout=COMMIT_TIMEOUT),
	                              call.exit_config_mode()]
	assert "still be in the candidate" not in log_of(logger)


def test_junos_failed_commit_shows_the_rollbacks_reply_and_asks_to_check():
	"""After a failed Junos commit, `rollback 0` is sent and what the device
	answered is shown - whether it was accepted isn't judged from its words:
	ACTION NEEDED, check the candidate configuration is empty."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	conn.send_config_set.side_effect = lambda cmds, **kw: (
		"error: syntax error" if cmds == ["rollback 0"]
		else "[edit system]\n-  host-name a;\n+  host-name b;" if cmds == ["show | compare"]
		else f"{cmds[0]}\nr1#")
	logger = fresh_logger()
	assert not push("juniper_junos", conn, logger=logger).applied
	log = log_of(logger)
	assert "ACTION NEEDED" in log
	assert "'rollback 0' (the device replied: error: syntax error), but 'show | compare' " \
	       "still shows: -  host-name a; | +  host-name b; - check the candidate " \
	       "configuration on the device" in log


def test_panos_falls_back_to_loading_the_running_config():
	"""When `revert config` is refused (older releases), PAN-OS loads the running config
	instead, and the log doesn't ask to discard the changes on the device."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	conn.send_config_set.side_effect = lambda cmds, **kw: (
		"Unknown command: revert" if cmds == ["revert config"] else "ok")
	logger = fresh_logger()
	assert not push("paloalto_panos", conn, logger=logger).applied
	assert call.send_config_set(["load config from running-config.xml"],
	                            exit_config_mode=False) in conn.method_calls
	assert "discard them on the device" not in log_of(logger)


def test_panos_failed_commit_sends_both_discards_and_asks_to_check():
	"""After a failed PAN-OS commit both discards are sent, the push isn't
	applied, and the log shows what the device answered and asks a person to
	check the candidate (whether a discard was accepted isn't judged from its
	words)."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	conn.send_config_set.side_effect = lambda cmds, **kw: (
		"Unknown command" if cmds[0] in ("revert config",
		                                 "load config from running-config.xml")
		else "deviceconfig { system { hostname fw9; } }" if cmds[0] == "run show config diff"
		else "ok")
	logger = fresh_logger()
	assert not push("paloalto_panos", conn, logger=logger).applied
	assert "'revert config' (the device replied: Unknown command), then 'load config from "\
	       "running-config.xml' (the device replied: Unknown command), but 'run show config " \
	       "diff' still shows: deviceconfig { system { hostname fw9; } } - check the " \
	       "candidate configuration on the device" in log_of(logger)


def test_junos_configures_privately():
	"""Junos enters config mode with `configure private`."""
	conn = connection()
	push("juniper_junos", conn)
	conn.config_mode.assert_called_once_with(config_command="configure private")


def test_other_platforms_use_the_drivers_config_mode():
	"""Other platforms (here IOS) enter the driver's default config mode, no arguments."""
	conn = connection()
	push("cisco_ios", conn)
	conn.config_mode.assert_called_once_with()


@pytest.mark.parametrize("vdoms, cfg_save, expected", [
	(False, "automatic", ["get system global | grep cfg-save"]),
	(False, "manual", ["get system global | grep cfg-save", "execute cfg save"]),
	(False, "revert", ["get system global | grep cfg-save", "execute cfg save"]),
	(True, "manual", ["config global", "get system global | grep cfg-save",
	                  "execute cfg save", "end"]),
])
def test_fortios_saves_only_when_cfg_save_isnt_automatic(vdoms, cfg_save,
                                                          expected):
	"""FortiOS checks cfg-save and runs `execute cfg save` only for manual or revert;
	with VDOMs the check and save are wrapped in `config global` ... `end`."""
	conn = connection(prompt="fw1 #", vdoms=vdoms, cfg_save=cfg_save)
	assert push("fortinet", conn).applied
	sent = [c.args[0] for c in conn.send_command_timing.call_args_list]
	assert sent == expected


def test_gaia_refuses_when_the_expert_shell_stays():
	"""Gaia whose prompt stays an expert/`#` shell: nothing is sent, the push isn't
	applied, and the log says the account has the wrong shell."""
	for prompt in ("[Expert@gw-1:0]#", "gw-1#"):
		conn = connection(prompt=prompt)
		logger = fresh_logger()
		assert push("checkpoint_gaia", conn, logger=logger) == PushResult(applied=False)
		conn.send_config_set.assert_not_called()     # nothing was sent
		assert "wrong shell" in log_of(logger)


def test_gaia_saves_with_its_own_command():
	"""Gaia leaves config mode, saves with `save config`, then reads its save
	state (`show config-state`)."""
	conn = connection(prompt="gw-1>")
	assert push("checkpoint_gaia", conn).applied
	assert finish_calls(conn) == [call.exit_config_mode(),
	                              call.send_command("save config"),
	                              call.send_command("show config-state")]


def test_fortios_closes_open_blocks_and_saves_nothing():
	"""FortiOS sends `end` for every open config block (read from the prompt), then
	checks cfg-save; no save_config, no commit."""
	conn = connection()
	conn.find_prompt.side_effect = ["fw1 (ipv6) #", "fw1 (port1) #",
	                                "fw1 (interface) #", "fw1 #"]
	assert push("fortinet", conn, commands=("config system interface",)).applied
	# every open block closed first, then the save mode is checked
	assert conn.send_command_timing.call_args_list == \
	       [call("end")] * 3 + [call("get system global | grep cfg-save")]
	conn.save_config.assert_not_called()
	conn.commit.assert_not_called()


def test_every_command_is_sent_and_each_reply_kept():
	"""Whatever the device answers, the following commands are still sent, and
	each command's reply is kept with it (shown, never judged)."""
	conn = connection()
	conn.send_config_set.side_effect = ["bad\n% Invalid input detected\nr1#", "good 1\nr1#", "good 2\nr1#"]
	result = push("cisco_ios", conn, commands=("bad", "good 1", "good 2"))
	assert result.applied
	assert result.replies == (("% Invalid input detected",), (), ())
	assert conn.send_config_set.call_count == 3


# ── The device status ────────────────────────────────────────────────────────

def run(conn, commands, verify, before=None, after=None):
	"""A whole rollout to one IOS device over `conn`: the configuration read
	before the push is `before`, every read after it `after` (or `before` -
	raising when one is an exception). The device's result."""
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	reads = iter([before] + [after if after is not None else before] * 5)

	def show(command, **_):
		value = next(reads)
		if isinstance(value, Exception):
			raise value
		return value or ""

	conn.send_command.side_effect = show
	engine = RolloutEngine(RolloutOptions(verify=verify), [device], commands)
	with patch("netmiko.ConnectHandler", return_value=conn):
		(result,) = engine.run(threading.Event(),
		                       RolloutLogger(webapp=False, verbose=False))
	return result


@pytest.mark.parametrize("replies", [["a\nr1#", "b\nr1#"], ["a\n% Invalid input\nr1#", "b\nr1#"]])
def test_without_verify_a_device_is_not_verified_whatever_it_replied(replies):
	"""Without verify nothing is judged: an applied device is not verified,
	whatever the device answered (its replies are kept)."""
	conn = connection()
	conn.send_config_set.side_effect = replies
	assert run(conn, ["a", "b"], verify=False)["status"] == "not verified"


def test_a_command_that_leaves_no_trace_is_review():
	"""A command that changes nothing in the configuration (`write memory`) isn't
	proven by it: review, with the other command's success counted."""
	result = run(connection(), ["hostname r1", "write memory"], verify=True,
	             before="hostname r0\n", after="hostname r1\n")
	assert result["status"] == "review"
	assert result["commands_verified"] == 1
	verdicts = [c["verdict"] for c in result["transcript"]["commands"]]
	assert verdicts == ["took effect", "review"]


def test_a_config_that_cant_be_read_is_not_verified():
	"""When the configuration can't be read before the push, the commands are
	still sent and the device is not verified (commands_verified None)."""
	result = run(connection(), ["hostname r1"], verify=True,
	             before=OSError("fetch timed out"))
	assert result["status"] == "not verified"
	assert result["commands_verified"] is None
	assert result["transcript"] is None


@pytest.mark.parametrize("after, status, verified", [
	("hostname r1\nntp server 1.1.1.1\n", "success", 2),
	("hostname r1\n", "review", 1),
	("hostname r0\n", "review", 0),
])
def test_with_verify_the_configuration_decides(after, status, verified):
	"""With verify, the configuration before and after decides: both commands
	there is success; anything not proven is review - with the proven count."""
	result = run(connection(), ["hostname r1", "ntp server 1.1.1.1"],
	             verify=True, before="hostname r0\n", after=after)
	assert (result["status"], result["commands_verified"]) == (status, verified)


def test_a_verified_device_keeps_its_diff_and_transcript():
	"""A verified device keeps its configuration diff and its transcript (per
	command: verdict, evidence, reply); the configuration after only when it's
	in review."""
	result = run(connection(), ["hostname r1"], verify=True,
	             before="hostname r0\n", after="hostname r1\n")
	assert "-hostname r0" in result["config_diff"] and "+hostname r1" in result["config_diff"]
	assert result["transcript"]["commands"] == [{"command": "hostname r1", "verdict": "took effect",
	                                             "evidence": ["+ hostname r1"], "note": "", "reply": []}]
	assert result["fetched_config"] is None


def test_failed_devices_are_not_judged():
	"""A device whose push failed (a failed Junos commit) is failed; its
	configuration is read before the push, never after."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	conn.send_command.return_value = "set system host-name j1\n"
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="juniper_junos", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(verify=True), [device], ["set x"])
	with patch("netmiko.ConnectHandler", return_value=conn):
		(result,) = engine.run(threading.Event(),
		                       RolloutLogger(webapp=False, verbose=False))
	assert result["status"] == "failed"
	shown = [c.args[0] for c in conn.send_command.call_args_list]
	assert shown == list(PLATFORMS["juniper_junos"].show_config)   # once: before


# ── Review fixes ─────────────────────────────────────────────────────────────

def test_a_commit_that_outlasts_the_wait_is_reported_honestly():
	"""A commit that times out is not applied, the log says it didn't finish within the
	wait, and the session is closed."""
	conn = connection()
	conn.commit.side_effect = netmiko.exceptions.ReadTimeout("slow")
	logger = fresh_logger()
	assert not push("paloalto_panos", conn, logger=logger).applied
	with open(logger.logfile, encoding="utf-8") as f:
		assert "didn't finish within" in f.read()
	conn.disconnect.assert_called_once()


def test_the_session_is_closed_when_the_push_fails_half_way():
	"""A connection error while sending makes the push not applied and still disconnects."""
	conn = connection()
	conn.send_config_set.side_effect = OSError("socket closed")
	assert not push("cisco_ios", conn).applied
	conn.disconnect.assert_called_once()


@pytest.mark.parametrize("device_type, under, line, commands, verdicts", INSERTS,
                         ids=[i[0] for i in INSERTS])
def test_the_whole_rollout_on_each_platform(device_type, under, line, commands, verdicts):
	"""push → the platform's finish → its configuration read before and after
	in the same session → the verdicts → the device status, with Netmiko mocked
	end to end: a change the commands made is success, the session uses the
	device's port, the platform's show commands are sent, and it commits or
	saves as its row says."""
	platform = PLATFORMS[device_type]
	before, after = CONFIGS[device_type], with_line(CONFIGS[device_type], under, line)
	conn = connection(prompt={"fortinet": "fw1 #",
	                          "checkpoint_gaia": "gw-1>"}.get(device_type, "r1#"))
	reads = iter([before] * len(platform.show_config) + [after] * 20)
	conn.send_command.side_effect = lambda command, **_: (
		"saved" if command == "show config-state" else
		after if command == platform.show_saved else next(reads))
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type=device_type, secret="", port=2222)
	engine = RolloutEngine(RolloutOptions(verify=True), [device], commands)
	with patch("netmiko.ConnectHandler", return_value=conn) as handler:
		(result,) = engine.run(threading.Event(),
		                       RolloutLogger(webapp=False, verbose=False))

	assert result["status"] == "success"
	assert [c["verdict"] for c in result["transcript"]["commands"]] == [v.value for v in verdicts]
	assert {c.kwargs["port"] for c in handler.call_args_list} == {2222}
	shown = [c.args[0] for c in conn.send_command.call_args_list
	         if c.args and c.args[0] in platform.show_config]
	assert shown == list(platform.show_config) * 2       # before and after
	assert conn.commit.called == isinstance(platform.finish, Commit)
	assert conn.save_config.called == isinstance(platform.finish, SaveConfig)
	assert result["action_needed"] is None               # saved, as checked


def test_each_command_is_sent_as_typed_without_reentering_config_mode():
	"""Each command is sent on its own with enter/exit_config_mode off. Netmiko's default
	re-checks config mode per call; Aruba CX's driver only recognises "(config)#", so
	inside "(config-if)#" it would try to enter config mode again and fail on the
	second command of a section."""
	conn = connection(prompt="a1(config-if)#")
	push("aruba_aoscx", conn, commands=("interface 1/1/1", "description x"))
	sends = [c for c in conn.send_config_set.call_args_list]
	assert len(sends) == 2
	assert all(c.kwargs == {"enter_config_mode": False,
	                        "exit_config_mode": False} for c in sends)


def test_junos_private_mode_refused_fails_with_the_likely_reason():
	"""When `configure private` isn't entered, nothing is sent, the push isn't applied,
	and the log names uncommitted changes in the shared configuration."""
	conn = connection()
	conn.config_mode.side_effect = netmiko.exceptions.ReadTimeout("pattern not found")
	logger = fresh_logger()
	assert push("juniper_junos", conn, logger=logger) == PushResult(applied=False)
	conn.send_config_set.assert_not_called()
	assert "uncommitted changes in the shared configuration" in log_of(logger)


def test_a_failed_save_is_reported_as_not_saved():
	"""A save refused by Gaia's config lock leaves the push applied (the change is live,
	but lost at the next reboot), with an ACTION NEEDED "NOT saved" line."""
	conn = connection(prompt="gw-1>")
	conn.send_command.return_value = ("CLINFR0771  Config lock is owned by admin. "
	                                  "Use the command 'lock database override'")
	logger = fresh_logger()
	assert push("checkpoint_gaia", conn, logger=logger).applied
	assert "ACTION NEEDED - 10.0.0.1:22: the change is live but NOT saved" in log_of(logger)


def test_a_successful_save_says_nothing_extra():
	"""A save answered [OK] logs no "NOT saved"."""
	conn = connection()
	conn.save_config.return_value = "Building configuration...\n[OK]"
	logger = fresh_logger()
	assert push("cisco_ios", conn, logger=logger).applied
	assert "NOT saved" not in log_of(logger)


def test_xr_route_policy_blocks_close_with_end_policy():
	"""On IOS XR, `end-policy` closes a route-policy block, so the router bgp commands
	after it are found at the top level."""
	config = ("route-policy PASS\n  pass\nend-policy\n!\n"
	          "router bgp 65000\n neighbor 10.0.0.2\n  remote-as 65001\n !\n!\n")
	outcome = judge(["route-policy PASS", "pass", "end-policy",
	                 "router bgp 65000", "neighbor 10.0.0.2", "remote-as 65001"], "", config)
	assert [j.verdict for j in outcome.commands] == [T, T, N, T, T, T]



def test_gaia_switches_an_expert_shell_to_clish():
	"""From an expert shell, Gaia's push sends `clish` and, once in clish, sends the command."""
	conn = connection()
	conn.find_prompt.side_effect = ["[Expert@gw-1:0]#", "gw-1>"]
	assert push("checkpoint_gaia", conn).applied
	conn.send_command_timing.assert_any_call("clish")
	conn.send_config_set.assert_called_once()        # the command went in


def test_gaia_fetch_switches_to_clish_too():
	"""Fetching Gaia's config from an expert shell sends `clish` first and returns
	the config."""
	conn = connection()
	conn.__enter__.return_value = conn
	conn.find_prompt.side_effect = ["[Expert@gw-1:0]#", "gw-1>"]
	conn.send_command.return_value = "set hostname gw-1"
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="checkpoint_gaia", secret="", port=22)
	with patch("netmiko.ConnectHandler", return_value=conn):
		config = NetmikoSession(device, RunReport(fresh_logger()))._fetch_new("in a test")
	assert config == "set hostname gw-1"
	conn.send_command_timing.assert_any_call("clish")


def test_a_new_hostname_is_saved_from_a_new_session():
	"""When the prompt changes mid-push (a new hostname), the push counts as applied and
	the save runs from a second session, as the log says."""
	first, second = connection(), connection()
	second.__enter__.return_value = second
	first.send_config_set.side_effect = ["ok", netmiko.exceptions.ReadTimeout("prompt")]
	logger = fresh_logger()
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["hostname r9", "x"])
	with patch("netmiko.ConnectHandler", side_effect=[first, second]):
		_, results = engine._push_config(threading.Event(), logger)
	assert results[0].applied
	first.save_config.assert_not_called()
	second.save_config.assert_called_once()
	assert "'save' sent from a new session" in log_of(logger)


def test_a_new_hostname_that_cant_be_saved_says_so():
	"""When the second session for the save can't connect, the push is still applied and
	the log says NOT saved."""
	first = connection()
	first.send_config_set.side_effect = netmiko.exceptions.ReadTimeout("prompt")
	logger = fresh_logger()
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["hostname r9"])
	with patch("netmiko.ConnectHandler", side_effect=[first, OSError("refused")]):
		_, results = engine._push_config(threading.Event(), logger)
	assert results[0].applied
	assert "NOT saved" in log_of(logger)



def stops_answering(replies, commands, logger=None):
	"""One cisco_ios rollout whose device gives `replies` (a reply, or an exception
	to raise) to the commands in turn. :returns: (its result, the first session, the
	ConnectHandler mock, the logger)"""
	first = connection()
	first.send_config_set.side_effect = replies
	logger = logger or fresh_logger()
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], commands)
	with patch("netmiko.ConnectHandler", side_effect=[first, connection()]) as handler:
		(result,) = engine.run(threading.Event(), logger)
	return result, first, handler, logger


def test_a_device_that_stops_answering_mid_list_is_partial_and_not_saved():
	"""A timeout on a command that isn't the last (a question with no prompt, a slow
	command): the commands after it aren't sent, nothing is saved, the device is
	"partial" with the commands sent counted and an ACTION NEEDED saying which
	command and that the earlier ones are live but NOT saved - not "applied" with
	the half-done config saved."""
	commands = ["logging host 1.1.1.1", "crypto key generate rsa",
	            "snmp-server community x RO", "ntp server 2.2.2.2"]
	result, first, handler, logger = stops_answering(
		["ok", netmiko.exceptions.ReadTimeout("no prompt")], commands)
	assert result["status"] == "partial"
	assert result["commands_sent"] == 2
	assert first.send_config_set.call_count == 2          # nothing after it
	first.save_config.assert_not_called()
	assert handler.call_count == 1                        # no second session to save
	assert "'crypto key generate rsa' (command 2 of 4)" in result["action_needed"]
	assert "NOT saved" in result["action_needed"]
	assert "ACTION NEEDED" in log_of(logger)


def test_a_refusal_before_a_mid_list_timeout_is_kept():
	"""What the device answered before it stopped answering is kept and logged;
	the device is "partial" (the commands after it weren't sent)."""
	commands = ["ntp server bogus", "logging host 1.1.1.1", "crypto key generate rsa",
	            "snmp-server community x RO"]
	result, _, _, logger = stops_answering(
		["% Invalid input detected at '^' marker.", "ok",
		 netmiko.exceptions.ReadTimeout("no prompt")], commands)
	assert result["status"] == "partial" and result["commands_sent"] == 3
	assert "'ntp server bogus' - the device replied: % Invalid input detected at '^' marker." in log_of(logger)


def test_a_timeout_on_the_first_command_is_failed():
	"""The first command already gets no answer: nothing confirmed, nothing else
	sent - "failed", with the ACTION NEEDED saying so."""
	result, first, _, _ = stops_answering(
		[netmiko.exceptions.ReadTimeout("no prompt")],
		["crypto key generate rsa", "logging host 1.1.1.1"])
	assert result["status"] == "failed"
	assert first.send_config_set.call_count == 1
	assert "(command 1 of 2)" in result["action_needed"]
	first.save_config.assert_not_called()


def test_a_new_hostname_after_a_refusal_keeps_the_reply():
	"""The last command's timeout (a new hostname) is still saved from a new
	session; without verify the device is not verified, and what it answered
	the command before is kept in the log."""
	result, _, handler, logger = stops_answering(
		["% Invalid input detected at '^' marker.", netmiko.exceptions.ReadTimeout("prompt")],
		["ntp server bogus", "hostname r9"])
	assert handler.call_count == 2                        # saved from a new session
	assert result["status"] == "not verified"
	assert "'ntp server bogus' - the device replied: % Invalid input" in log_of(logger)

# ── Cases only a person can resolve: unmistakable, and in the summary ──

@pytest.mark.parametrize("device_type, setup, words", [
	("juniper_junos",          # another admin's uncommitted shared edits
	 lambda c: setattr(c.config_mode, "side_effect",
	                   __import__("netmiko").exceptions.ReadTimeout("x")),
	 "uncommitted changes in the shared configuration"),
	("paloalto_panos",         # failed commit, both discards refused
	 lambda c: (setattr(c.commit, "side_effect", ValueError("Commit failed")),
	            setattr(c.send_config_set, "side_effect",
	                    lambda cmds, **kw: "Unknown command"
	                    if cmds[0] in ("revert config",
	                                   "load config from running-config.xml")
	                    else "ok")),
	 "check the candidate configuration on the device"),
	("paloalto_panos",         # commit still running after the wait
	 lambda c: setattr(c.commit, "side_effect",
	                   __import__("netmiko").exceptions.ReadTimeout("slow")),
	 "check on the device"),
	("checkpoint_gaia",        # stuck in expert even after "clish"
	 lambda c: setattr(c.find_prompt, "return_value", "[Expert@gw-1:0]#"),
	 "set its shell to clish"),
	("checkpoint_gaia",        # save refused: config lock
	 lambda c: (setattr(c.find_prompt, "return_value", "gw-1>"),
	            setattr(c.send_command, "return_value",
	                    "CLINFR0771  Config lock is owned by admin.")),
	 "NOT saved"),
])
def test_manual_cases_are_flagged_action_needed(device_type, setup, words):
	"""Each case only a person can resolve (Junos shared edits, PAN-OS discards refused or
	a slow commit, Gaia stuck in expert or its save locked) logs an ACTION NEEDED line
	with the instruction, and the summary counts the device."""
	conn = connection()
	setup(conn)
	logger = fresh_logger()
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type=device_type, secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["set x"])
	with patch("netmiko.ConnectHandler", return_value=conn):
		engine.run(threading.Event(), logger)
	log = log_of(logger)
	flagged = [l for l in log.splitlines() if "ACTION NEEDED - 10.0.0.1:22:" in l]
	assert flagged and words in flagged[0]
	assert "ACTION NEEDED on 1 device (10.0.0.1:22)" in log     # the summary


def test_no_action_line_when_nothing_needs_a_person():
	"""A clean rollout logs no ACTION NEEDED at all."""
	logger = fresh_logger()
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["hostname r1"])
	with patch("netmiko.ConnectHandler", return_value=connection()):
		engine.run(threading.Event(), logger)
	assert "ACTION NEEDED" not in log_of(logger)


def test_the_instruction_travels_in_the_device_result():
	"""The instruction is in the device result's action_needed (what the Results page
	shows); the device is applied (not verified: no verify) - saving is the issue."""
	conn = connection(prompt="gw-1>")
	conn.send_command.return_value = "CLINFR0771  Config lock is owned by admin."
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="checkpoint_gaia", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["set hostname x"])
	with patch("netmiko.ConnectHandler", return_value=conn):
		(result,) = engine.run(threading.Event(), fresh_logger())
	assert result["action_needed"].startswith("the change is live but NOT saved")
	assert result["status"] == "not verified"       # applied; saving is the issue


def test_no_instruction_in_a_clean_result():
	"""A clean device result has action_needed None."""
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["hostname r1"])
	with patch("netmiko.ConnectHandler", return_value=connection()):
		(result,) = engine.run(threading.Event(), fresh_logger())
	assert result["action_needed"] is None


# ── The Finish kinds' contracts ──────────────────────────────────────────────

class Recorder:
	"""A report that records what a finish tells it."""
	def __init__(self):
		self.lines, self.actions = [], []

	def notify(self, text, tone, important=False):
		self.lines.append(text)

	def action_needed(self, device, what):
		self.actions.append((device.endpoint, what))


TARGET = Device(ip="10.0.0.1", label="d", username="u", password="p",
                device_type="cisco_ios", secret="", port=22)


def test_only_a_commit_is_not_interruptible():
	"""A device that stops answering mid-push keeps what it took unless the platform
	commits: SaveConfig, RunCommand and NoFinish are interruptible, Commit isn't."""
	assert [f.interruptible for f in (SaveConfig(), RunCommand("save config"),
	                                  NoFinish(), Commit())] == [True, True, True, False]


def test_the_finish_after_leaving_config_mode():
	"""After leaving config mode: SaveConfig saves (reply, "save"), RunCommand sends its
	command (reply, the command), NoFinish and Commit send nothing ("", "")."""
	conn = MagicMock()
	conn.save_config.return_value = "[OK]"
	conn.send_command.return_value = "done"
	assert SaveConfig().after_leave(conn) == ("[OK]", "save")
	assert RunCommand("save config").after_leave(conn) == ("done", "save config")
	conn.send_command.assert_called_once_with("save config")
	assert NoFinish().after_leave(conn) == ("", "")
	assert Commit().after_leave(conn) == ("", "")


def test_finishing_in_a_new_session():
	"""From a fresh session, SaveConfig and RunCommand enable then finish as after
	leaving config mode; NoFinish opens no session and answers None."""
	conn = MagicMock()
	conn.__enter__.return_value = conn
	conn.save_config.return_value = "[OK]"
	connect = MagicMock(return_value=conn)
	assert SaveConfig().in_new_session(connect) == ("[OK]", "save")
	assert [c[0] for c in conn.method_calls] == ["enable", "save_config"]
	assert RunCommand("save config").in_new_session(connect)[1] == "save config"
	unused = MagicMock()
	assert NoFinish().in_new_session(unused) is None
	unused.assert_not_called()


def test_only_a_commit_acts_before_leaving_config_mode():
	"""Before leaving config mode the save / command / nothing kinds do nothing and go
	on (True); a commit that works commits once and goes on, telling nothing."""
	for finish in (SaveConfig(), RunCommand("save config"), NoFinish()):
		conn, report = MagicMock(), Recorder()
		assert finish.before_leave(conn, report, TARGET) is True
		assert conn.method_calls == [] and report.actions == []
	conn, report = MagicMock(), Recorder()
	assert Commit(discard=("rollback 0",)).before_leave(conn, report, TARGET) is True
	assert conn.method_calls == [call.commit(read_timeout=COMMIT_TIMEOUT)]
	assert (report.lines, report.actions) == ([], [])


def test_a_commit_that_reverts_on_failure_asks_nothing_of_a_person():
	"""A failed commit on a platform that drops it by itself sends no discard, leaves
	config mode and asks no one to discard anything (False: not applied)."""
	conn, report = MagicMock(), Recorder()
	conn.commit.side_effect = ValueError("Commit failed")
	assert Commit(reverts_on_failure=True).before_leave(conn, report, TARGET) is False
	assert conn.method_calls == [call.commit(read_timeout=COMMIT_TIMEOUT),
	                             call.exit_config_mode()]
	assert report.actions == []


def test_a_commit_without_a_discard_that_fails_asks_to_discard():
	"""A failed commit with no discard command and no revert of its own asks a person
	to discard the candidate on the device."""
	conn, report = MagicMock(), Recorder()
	conn.commit.side_effect = ValueError("Commit failed")
	assert Commit().before_leave(conn, report, TARGET) is False
	assert [where for where, _ in report.actions] == ["10.0.0.1:22"]
	assert "still be in the candidate" in report.actions[0][1]


def test_a_commit_that_outlasts_the_wait_stays_in_config_mode_untouched():
	"""A commit still running after the wait claims nothing (CommitPending: the session
	decides by the configuration) - and nothing else is sent (no discard, config mode not
	left)."""
	conn, report = MagicMock(), Recorder()
	conn.commit.side_effect = netmiko.exceptions.ReadTimeout("slow")
	with pytest.raises(CommitPending):
		Commit(discard=("rollback 0",)).before_leave(conn, report, TARGET)
	assert conn.method_calls == [call.commit(read_timeout=COMMIT_TIMEOUT)]
	assert report.actions == []


def slow_commit(configs):
	"""One verified Junos rollout of `set x` whose commit times out; the configuration
	reads give `configs` in turn (before, then each new session's). :returns: (its result,
	the log, the waits between the reads)"""
	conn = connection(prompt="u@j1#")
	conn.__enter__.return_value = conn
	conn.commit.side_effect = netmiko.exceptions.ReadTimeout("slow")
	reads = iter(configs)
	conn.send_command.side_effect = lambda command, **kw: next(reads)
	logger = fresh_logger()
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="juniper_junos", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(verify=True), [device], ["set x"])
	with patch("netmiko.ConnectHandler", return_value=conn), \
			patch("src.rollout.session.time.sleep") as wait:
		(result,) = engine.run(threading.Event(), logger)
	return result, log_of(logger), [c.args[0] for c in wait.call_args_list]


def test_a_slow_commit_that_went_through_is_judged_by_the_configuration():
	"""A commit that times out, after which a new session finds the configuration
	changed: the push went through - judged as usual (success), no ACTION NEEDED."""
	before, after = "set system host-name j1\n", "set system host-name j1\nset x\n"
	result, log, waits = slow_commit([before, before, after])
	assert waits == [30]                                  # found on the second read
	assert result["status"] == "success"
	assert [c["verdict"] for c in result["transcript"]["commands"]] == ["took effect"]
	assert result["action_needed"] is None
	assert "the commit didn't finish within 300s, and the configuration has changed since" in log


def test_a_slow_commit_that_changed_nothing_is_failed_for_a_person():
	"""A commit that times out and whose configuration still hasn't changed after every
	check: failed, with an ACTION NEEDED saying it failed or is still running."""
	before = "set system host-name j1\n"
	result, _, waits = slow_commit([before] * 6)
	assert waits == [30] * 4                              # 5 reads, 30 s apart
	assert result["status"] == "failed"
	assert "still hadn't changed 120s later - it failed or is still running" in result["action_needed"]


def test_a_command_that_gets_no_prompt_keeps_the_devices_question():
	"""A command whose answer is a question (no prompt comes back) times out: what the
	device printed is kept from the session's record - logged, in the ACTION NEEDED and
	as the command's reply."""
	first = connection()
	opened = {}

	def connect(**kwargs):
		opened.update(kwargs)
		return first

	def send(cmds, **kw):
		if cmds == ["crypto key zeroize rsa"]:
			opened["session_log"].write(
				b"crypto key zeroize rsa\n% All keys will be removed.\n"
				b"Do you really want to remove these keys? [yes/no]: ")
			raise netmiko.exceptions.ReadTimeout("no prompt")
		return f"{cmds[0]}\nr1#"
	first.send_config_set.side_effect = send
	logger = fresh_logger()
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device],
	                       ["logging host 1.1.1.1", "crypto key zeroize rsa", "ntp server 2.2.2.2"])
	with patch("netmiko.ConnectHandler", side_effect=connect):
		(result,) = engine.run(threading.Event(), logger)
	question = "% All keys will be removed. | Do you really want to remove these keys? [yes/no]:"
	assert result["status"] == "partial"
	assert f"(command 2 of 3; its last output: {question})" in result["action_needed"]
	assert f"'crypto key zeroize rsa' - no prompt came back; the device's last output: " \
	       f"{question}" in log_of(logger)


# ── Without Verify: what a device said is still in sight ─────────────────────

def unverified_rollout(answers):
	"""One cisco_ios rollout without verify whose device answers each command with
	`answers` (by command). :returns: (its result, [(line, important)] it logged)"""
	conn = connection()
	conn.send_config_set.side_effect = lambda cmds, **kw: answers.get(cmds[0], f"{cmds[0]}\nr1#")
	notifier = MagicMock(logfile="rollout.log")
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(verify=False), [device], ["ntp server bogus", "logging host 1.1.1.1"])
	with patch("netmiko.ConnectHandler", return_value=conn):
		(result,) = engine.run(threading.Event(), notifier)
	return result, [(c.args[0], c.kwargs.get("important", False)) for c in notifier.notify.call_args_list]


def test_without_verify_a_reply_reaches_the_console_and_the_summary():
	"""Verify off: a command the device answered (here refused) is logged as
	important - the CLI shows it without -v - and the summary names the device;
	the device stays not verified (the reply is never judged). A quiet device
	isn't named."""
	result, lines = unverified_rollout({"ntp server bogus": "% Invalid input detected at '^' marker.\nr1(config)#"})
	assert result["status"] == "not verified"
	assert ("10.0.0.1:22: 'ntp server bogus' - the device replied: % Invalid input detected at "
	        "'^' marker.", True) in lines
	assert ("1 device not verified replied to some commands (10.0.0.1:22) - read the lines "
	        "marked 'the device replied': without Verify NetRollout can't tell a refusal "
	        "from a notice", True) in lines
	# a device that answered nothing gets no such line
	_, quiet = unverified_rollout({})
	assert not any("replied to some commands" in line for line, _ in quiet)


# ── v4.2: each platform's grammar declared in full ──

def test_every_platform_declares_its_exec_words_defaults_and_event_shapes():
	"""All 12 rows say it - a platform with no way to show defaults says so
	(no_defaults: its unproven commands stay in review)."""
	for name, platform in PLATFORMS.items():
		assert platform.exec_words, name
		assert callable(platform.defaults), name
		assert platform.events.match("*Oct 19 19:17:26.223: %LINK-3-UPDOWN: x"), name
	assert [n for n, p in PLATFORMS.items() if p.defaults is no_defaults] == [
		"cisco_xr", "hp_procurve", "hp_comware", "juniper_junos", "paloalto_panos", "checkpoint_gaia"]
	assert PLATFORMS["cisco_ios"].defaults([("interface Loopback1",)]) == ["show running-config all"]


def test_no_exec_word_begins_a_configuration_command_of_its_platform():
	"""An exec word hides a command from judging: none of the words is the
	first word of a configuration command typed in the lab corpus."""
	typed = {"cisco_ios": ["interface", "ip", "no", "snmp-server", "username", "ntp", "crypto", "description"],
	         "cisco_nxos": ["interface", "ip", "no", "snmp-server", "feature", "vlan", "username"],
	         "fortinet": ["config", "edit", "set", "unset", "delete", "next", "end"]}
	for name, words in typed.items():
		assert not set(words) & set(PLATFORMS[name].exec_words), name


def test_fortios_defaults_are_fetched_per_block_with_names_as_printed():
	sections = [("config system global",), ("config firewall address", 'edit "My Addr"'),
	            ("config system global",), ("config vdom", "edit root"),
	            ("config system interface", 'edit "port1"', "config ipv6"), ()]
	assert fortios_blocks(sections) == ["show full-configuration system global",
	                                    'show full-configuration firewall address "My Addr"']


@pytest.mark.parametrize("line, event", [
	("*Oct 19 19:17:26.223: %LINK-3-UPDOWN: Interface Loopback99, changed state to up", True),
	("*00:12:34: %LINK-3-UPDOWN: Interface Loopback99, changed state to up", True),
	("1w2d: %SYS-5-CONFIG_I: Configured from console", True),
	("%LINEPROTO-5-UPDOWN: Line protocol on Interface Loopback99, changed state to up", True),
	("2026 Oct 19 16:10:28 NXOS1 %ETHPORT-5-IF_UP: Interface loopback95 is up", True),
	("2026-10-21T10:00:00.123+00:00 sw1 Ebra: link up", True),
	# answers - never events
	("% Initialization of NTP failed with status 4.", False),
	("%NTP: failed to initialize NTP process", False),
	("% Invalid input detected at '^' marker.", False),
	("Unknown action 0", False),
])
def test_event_shapes(line, event):
	assert bool(EVENTS.match(line)) is event


@pytest.mark.parametrize("platform, line", [
	("hp_comware", "%Oct 21 10:00:00:123 2026 HP IFNET/3/PHY_UPDOWN: Physical state on the interface changed"),
	("hp_procurve", "I 10/21/26 10:00:00 00076 ports: port 1 is now on-line"),
	("cisco_xr", "RP/0/RP0/CPU0:Oct 21 10:00:00.123 UTC: ifmgr[257]: %PKT_INFRA-LINK-3-UPDOWN : x"),
])
def test_a_platforms_own_event_shapes(platform, line):
	"""Told apart as an event on its platform - so a log line arriving with the
	next command's output is never taken for that command's reply."""
	said, events = reply(f"x\n{line}\nR1#", "x", "R1", PLATFORMS[platform].events)
	assert (said, events) == ([], [line])


def run_with_defaults(commands, before, after, defaults, replies=None):
	"""A verified rollout to one IOS device whose "show running-config all"
	answers `defaults` (a value, or an exception raised). The device's result
	and the commands its configuration was read with."""
	conn = connection()
	if replies is not None:
		conn.send_config_set.side_effect = replies
	reads = iter([before] + [after] * 5)
	sent = []

	def show(command, **_):
		sent.append(command)
		if command == "show running-config all":
			if isinstance(defaults, Exception):
				raise defaults
			return defaults
		return next(reads)

	conn.send_command.side_effect = show
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(verify=True), [device], commands)
	with patch("netmiko.ConnectHandler", return_value=conn):
		(result,) = engine.run(threading.Event(), RolloutLogger(webapp=False, verbose=False))
	return result, sent


LO = "interface Loopback1\n ip address 1.1.1.1 255.255.255.255\n"


def test_the_defaults_are_read_only_when_a_command_is_left_unproven():
	result, sent = run_with_defaults(["hostname r1"], "hostname r0\n", "hostname r1\n", "unused")
	assert result["status"] == "success" and "show running-config all" not in sent


def test_an_unproven_command_is_proven_by_the_configuration_with_defaults():
	"""IOS hides "ip redirects" (its default): read with defaults, it's in place."""
	result, sent = run_with_defaults(["interface Loopback1", "ip redirects"], LO, LO,
	                                 LO.replace(" ip address", " ip redirects\n ip address"))
	assert sent.count("show running-config all") == 1
	assert (result["status"], result["commands_verified"]) == ("success", 2)
	assert result["transcript"]["commands"][1]["note"] == "shown with the device's defaults"


@pytest.mark.parametrize("defaults", [OSError("timed out"), "x\n" * 50_001], ids=["unreadable", "too long"])
def test_defaults_that_cant_be_read_or_are_too_long_leave_the_command_as_it_was(defaults):
	"""Unreadable, or longer than DEFAULTS_MAX_LINES: not judged - the command
	stays unproven (review), the device verified all the same."""
	result, _ = run_with_defaults(["interface Loopback1", "ip redirects"], LO, LO, defaults)
	assert result["status"] == "review"


def test_a_device_with_a_rejected_command_and_a_proven_one_is_partial():
	"""Some commands proven, one rejected (the device replied, nothing shows it):
	partial - failed only when nothing was proven."""
	replies = ["hostname r1\nr1#", "ntp server 1.1.1.1\n% NTP failed\nr1#"]
	result, _ = run_with_defaults(["hostname r1", "ntp server 1.1.1.1"], "hostname r0\n",
	                              "hostname r1\n", "hostname r1\n", replies)
	assert (result["status"], result["commands_verified"]) == ("partial", 1)
	assert [c["verdict"] for c in result["transcript"]["commands"]] == ["took effect", "rejected"]


# ── B: what a device not judged replied, stored for Results ──

def test_without_verify_the_replies_are_stored_redacted_for_the_commands_answered():
	"""Not judged: each command the device answered with its reply - redacted as
	the log is (the command too); a command it was silent to isn't listed."""
	conn = connection()
	conn.send_config_set.side_effect = ["a\nr1#", "snmp-server community s3cret RO\n% Invalid input\nr1#"]
	result = run(conn, ["a", "snmp-server community s3cret RO"], verify=False)
	assert result["replies"] == [{"command": "snmp-server community <redacted> RO",
	                              "reply": ["% Invalid input"]}]


def test_a_silent_device_or_a_judged_one_stores_no_replies():
	"""Nothing to show when the device said nothing; a judged device's replies
	are in its transcript already."""
	assert run(connection(), ["a", "b"], verify=False)["replies"] is None
	conn = connection()
	conn.send_config_set.side_effect = ["hostname r1\n% note\nr1#"]
	judged = run(conn, ["hostname r1"], verify=True, before="hostname r0\n", after="hostname r1\n")
	assert judged["replies"] is None and judged["transcript"]["commands"][0]["reply"] == ["% note"]
