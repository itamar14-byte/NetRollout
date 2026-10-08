"""Stage 4b: every supported platform — how the push finishes, how a refused
command is recognised, and the verify verdicts against the config as each
platform prints it (fixtures approximate real output; the EVE-NG round is
the real-device check)."""
import threading
import uuid
from unittest.mock import MagicMock, call, patch

import netmiko
import pytest

from src.rollout import inputs
from src.rollout.engine import Device, PushResult, RolloutEngine, RolloutOptions
from src.rollout.log import RolloutLogger
from src.rollout.platforms import COMMIT_TIMEOUT, NOT_CONFIGURED, PLATFORMS, STILL_CONFIGURED, UNVERIFIABLE, VARIABLE, VERIFIED, first_word, rejection, verify_commands


OK, MISSING, STILL, NV = VERIFIED, NOT_CONFIGURED, STILL_CONFIGURED, UNVERIFIABLE


def test_every_supported_platform_has_a_row():
	"""PLATFORMS has a row for exactly the supported platforms, no more, no fewer."""
	assert set(PLATFORMS) == set(inputs.SUPPORTED_PLATFORMS)


# ── Verify verdicts, per platform ────────────────────────────────────────────
# (device type, config as the platform prints it, [(typed command, verdict)])

IOS_CONFIG = """Building configuration...

Current configuration : 1980 bytes
!
version 17.3
hostname r1
!
no ip domain lookup
!
interface GigabitEthernet1
 description uplink-to-core
 ip address 10.0.0.1 255.255.255.0
 negotiation auto
!
interface GigabitEthernet2
 no ip address
 shutdown
!
router bgp 65000
 bgp log-neighbor-changes
 neighbor 10.0.0.2 remote-as 65001
 !
 address-family ipv4
  network 10.0.0.0 mask 255.255.255.0
  neighbor 10.0.0.2 activate
 exit-address-family
!
ip route 0.0.0.0 0.0.0.0 10.0.0.254
ntp server 1.1.1.1
banner motd ^C
Authorized access only
^C
!
end
"""

CASES = [
	("cisco_ios", IOS_CONFIG, [
		("hostname r1", OK),
		("interface GigabitEthernet1", OK),
		("description uplink-to-core", OK),
		("no shutdown", OK),                            # not shut: in effect
		("exit", NV),
		("interface GigabitEthernet2", OK),
		("description never-applied", MISSING),
		("no shutdown", STILL),                         # still shut down
		("router bgp 65000", OK),
		("address-family ipv4", OK),
		("neighbor 10.0.0.2 activate", OK),             # inside the AF section
		("exit-address-family", NV),
		("neighbor 10.0.0.2 remote-as 65001", OK),      # back at bgp level
		("ip route 0.0.0.0 0.0.0.0 10.0.0.254", OK),    # global, typed in bgp
		("no ip domain lookup", OK),                    # shown literally
		("no ntp server 9.9.9.9", OK),                  # absent: in effect
		("no ntp server 1.1.1.1", STILL),
		("end", NV),
		("write memory", NV),
	]),
	("cisco_xe", IOS_CONFIG, [
		("interface GigabitEthernet1", OK),
		("ip address 10.0.0.1 255.255.255.0", OK),
		("ip address 10.9.9.9 255.255.255.0", MISSING),
	]),
	("cisco_nxos", """!Command: show running-config
!Running configuration last done at: Thu Oct  1 10:00:00 2026

version 9.3(8) Bios:version
hostname n1
feature ospf
feature interface-vlan

vlan 1,10
vlan 10
  name users

interface Vlan10
  no shutdown
  ip address 10.10.0.1/24

interface Ethernet1/1
  description to-n2
  switchport access vlan 10

interface Ethernet1/2
  shutdown

router ospf 1
  router-id 1.1.1.1
""", [
		("feature ospf", OK),
		("feature bgp", MISSING),
		("vlan 10", OK),
		("name users", OK),
		("interface Vlan10", OK),
		("no shutdown", OK),                            # NX-OS shows it
		("shutdown", MISSING),
		("interface Ethernet1/2", OK),
		("no shutdown", STILL),
		("router ospf 1", OK),
		("router-id 1.1.1.1", OK),
	]),
	("cisco_xr", """Building configuration...
!! IOS XR Configuration 7.3.2
!! Last configuration change at Thu Oct  1 10:00:00 2026 by admin
!
hostname x1
interface GigabitEthernet0/0/0/0
 description core
 ipv4 address 10.1.1.1 255.255.255.252
!
interface GigabitEthernet0/0/0/1
 shutdown
!
router ospf 1
 area 0
  interface GigabitEthernet0/0/0/0
   cost 10
  !
 !
!
end
""", [
		("interface GigabitEthernet0/0/0/0", OK),
		("description core", OK),
		("router ospf 1", OK),
		("area 0", OK),
		("interface GigabitEthernet0/0/0/0", OK),       # the one inside area 0
		("cost 10", OK),
		("cost 20", MISSING),
		("root", NV),                                   # XR: back to the top
		("commit", NV),
	]),
	("arista_eos", """! Command: show running-config
! device: e1 (vEOS-lab, EOS-4.27.0F)
!
hostname e1
!
interface Ethernet1
   description srv
   switchport access vlan 20
!
ip routing
!
router bgp 65001
   neighbor 10.0.0.2 remote-as 65002
!
end
""", [
		("interface Ethernet1", OK),
		("description srv", OK),
		("switchport access vlan 30", MISSING),
		("router bgp 65001", OK),
		("neighbor 10.0.0.2 remote-as 65002", OK),
		("no ip routing", STILL),
	]),
	("aruba_aoscx", """Current configuration:
!
!Version ArubaOS-CX GL.10.08.1010
!export-password: default
hostname a1
vlan 1,10
vlan 10
    name users
interface 1/1/1
    no shutdown
    description access
    vlan access 10
""", [
		("vlan 10", OK),
		("name users", OK),
		("interface 1/1/1", OK),
		("description access", OK),
		("no shutdown", OK),
		("vlan access 20", MISSING),
	]),
	("hp_procurve", """Running configuration:

; J9773A Configuration Editor; Created on release #YA.16.10.0010
; Ver #14:01.4f.f8.1d.9b.3f.bf.bb.ef.7c.59.fc.6b.fb.9f.fc.ff.ff.37.ef:05

hostname "p1"
module 1 type j9773a
vlan 1
   name "DEFAULT_VLAN"
   untagged 1-23
   ip address dhcp-bootp
   exit
vlan 10
   name "users"
   tagged 24
   exit
""", [
		('hostname "p1"', OK),
		("hostname p1", OK),                            # quotes don't matter
		("vlan 10", OK),
		("name users", OK),
		("tagged 24", OK),
		("untagged 5", MISSING),
		("exit", NV),
	]),
	("hp_comware", """#
 version 7.1.064, Release 0427P22
#
 sysname c1
#
vlan 10
 name users
#
interface GigabitEthernet1/0/1
 port link-mode bridge
 description to-core
 port access vlan 10
#
interface GigabitEthernet1/0/2
 port link-mode bridge
 shutdown
#
return
""", [
		("sysname c1", OK),                             # top level after '#'
		("vlan 10", OK),
		("name users", OK),
		("quit", NV),
		("interface GigabitEthernet1/0/1", OK),
		("description to-core", OK),
		("port access vlan 20", MISSING),
		("quit", NV),
		("interface GigabitEthernet1/0/2", OK),
		("undo shutdown", STILL),                       # Comware's "no"
		("undo description", OK),                       # none there
		("return", NV),
	]),
	("juniper_junos", """set version 21.4R1.12
set system host-name j1
set system services ssh
set interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/24
set protocols ospf area 0.0.0.0 interface ge-0/0/0.0
set routing-options static route 0.0.0.0/0 next-hop 10.0.0.254
""", [
		("set system host-name j1", OK),
		("edit interfaces ge-0/0/0", NV),
		("set unit 0 family inet address 10.0.0.1/24", OK),   # relative to edit
		("set unit 0 family inet address 10.0.9.1/24", MISSING),
		("top", NV),
		("delete protocols bgp", OK),                   # absent: in effect
		("delete system services ssh", STILL),
		("set routing-options static route 0.0.0.0/0 next-hop 10.0.0.254", OK),
		("commit", NV),
	]),
	("paloalto_panos", """set deviceconfig system hostname fw1
set network interface ethernet ethernet1/1 layer3 ip 10.0.0.1/24
set zone trust network layer3 ethernet1/1
""", [
		("set deviceconfig system hostname fw1", OK),
		("edit network interface ethernet ethernet1/1", NV),
		("set layer3 ip 10.0.0.1/24", OK),
		("up", NV),
		("top", NV),
		("delete zone dmz", OK),
		("delete zone trust", STILL),
		("commit", NV),
	]),
	("checkpoint_gaia", """#
# Configuration of gw-1
# Language version: 14.1v1
#
# Exported by admin on Thu Oct  1 10:00:00 2026
#
set hostname gw-1
set interface eth1 ipv4-address 10.0.0.1 mask-length 24
set interface eth1 state on
add user ops uid 2001 homedir /home/ops
set static-route default nexthop gateway address 10.0.0.254 on
""", [
		("set hostname gw-1", OK),
		("set interface eth1 state on", OK),
		("set interface eth1 comments uplink", MISSING),
		("add user ops uid 2001 homedir /home/ops", OK),
		("delete user ops", STILL),                     # still an "add" line
		("delete user old", OK),
		("set static-route 10.9.0.0/16 off", OK),            # no such route
		("set static-route default off", STILL),             # still there
		("save config", NV),
	]),
	("fortinet", """#config-version=FGVM64-7.2.4-FW-build1396-230131:opmode=0:vdom=0:user=admin
#conf_file_ver=12345
#buildno=1396
#global_vdom=1
config system global
    set hostname "fw1"
    set timezone 28
end
config system interface
    edit "port1"
        set vdom "root"
        set ip 10.0.0.1 255.255.255.0
        set allowaccess ping https ssh
        config ipv6
            set ip6-address 2001:db8::1/64
        end
    next
    edit "port2"
        set vdom "root"
        set ip 10.1.0.1 255.255.255.0
    next
end
config firewall address
    edit "srv1"
        set subnet 10.2.0.10 255.255.255.255
    next
end
""", [
		("config system interface", OK),
		('edit "port1"', OK),
		("set allowaccess ping https ssh", OK),
		("config ipv6", OK),
		("set ip6-address 2001:db8::1/64", OK),
		("end", NV),                                    # closes only config ipv6
		("set description uplink", MISSING),            # still in edit port1
		("set ip 10.0.0.1 255.255.255.0", OK),          # ...as this one shows
		("next", NV),
		("edit port2", OK),                             # unquoted is fine
		("unset allowaccess", OK),                      # none there
		("next", NV),
		("end", NV),
		("config firewall address", OK),
		("delete old-srv", OK),
		("delete srv1", STILL),
		("end", NV),
		("config system global", OK),
		("set hostname fw1", OK),
	]),
]


@pytest.mark.parametrize("device_type, config, expected", CASES,
                         ids=[c[0] for c in CASES])
def test_verdicts(device_type, config, expected):
	"""On each platform's printed config, every typed command gets its expected verdict:
	verified, not configured, still configured, or not checkable (navigation, save)."""
	commands = [command for command, _ in expected]
	got = verify_commands(device_type, config, commands)
	assert list(zip(commands, got)) == expected


def test_unresolved_variables_are_not_judged():
	"""A command still holding a $$VARIABLE$$ gets the VARIABLE verdict, not a judgement."""
	verdicts = verify_commands("cisco_ios", IOS_CONFIG,
	                           ["hostname $$HOSTNAME$$", "hostname r1"])
	assert verdicts == [VARIABLE, OK]


@pytest.mark.parametrize("command, word", [
	('""', ""), ('" "', ""), ("", ""), ("   ", ""),
	('  "Exit" now', "exit"), ("Hostname R1", "hostname"),
])
def test_first_word_of_a_command(command, word):
	"""A command's first word as compared; a line that normalizes to nothing
	(blank, or only quotes) has the empty word instead of raising."""
	assert first_word(command) == word


# ── A refused command, in each vendor's words ────────────────────────────────

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
	# found in the vendor documentation (2026-10-02)
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

def test_rejections_are_recognised(command, output):
	"""Each vendor's error reply (IOS, NX-OS, EOS, Aruba, ProCurve, Comware, Junos, PAN-OS,
	Gaia, FortiOS) is recognised as a rejection of the command."""
	assert rejection(output, command)


def test_a_hostname_like_a_gaia_code_isnt_an_error():
	"""A prompt that looks like a Gaia error code (FWPROD0001>) isn't a rejection."""
	assert rejection("FWPROD0001> set hostname x", "set hostname x") is None


@pytest.mark.parametrize("command, output", [
	("description unknown-host", "r1(config-if)#description unknown-host\nr1(config-if)#"),
	("set comments invalid-input-filter", "fw1 (port1) # set comments invalid-input-filter\nfw1 (port1) #"),
	("hostname r2", "r2(config)#"),
])
def test_an_accepted_command_echo_is_not_a_rejection(command, output):
	"""A command echoed back with error-like words in its own text (unknown, invalid),
	or a bare new prompt, isn't a rejection."""
	assert rejection(output, command) is None


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
	conn.send_config_set.return_value = "ok"
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
                                         if p.finish == "save"])
def test_save_platforms_leave_config_mode_then_save(device_type):
	"""Every "save" platform leaves config mode, then saves (Aruba CX sends `end` first);
	the push is applied with nothing rejected."""
	conn = connection()
	assert push(device_type, conn) == PushResult(applied=True, rejected=0)
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
	"""A failed Junos commit is followed by `rollback 0` and leaving config mode; the push
	is reported not applied."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed: error: x")
	result = push("juniper_junos", conn)
	assert result == PushResult(applied=False, rejected=0)
	assert finish_calls(conn) == [
		call.commit(read_timeout=COMMIT_TIMEOUT),
		call.send_config_set(["rollback 0"], exit_config_mode=False),
		call.exit_config_mode()]


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


def test_panos_both_discards_refused_says_to_discard_on_the_device():
	"""When both PAN-OS discards are refused, the push isn't applied and the log says to
	discard the changes on the device."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	conn.send_config_set.side_effect = lambda cmds, **kw: (
		"Unknown command" if cmds[0] in ("revert config",
		                                 "load config from running-config.xml")
		else "ok")
	logger = fresh_logger()
	assert not push("paloalto_panos", conn, logger=logger).applied
	assert "discard them on the device" in log_of(logger)


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
		assert push("checkpoint_gaia", conn, logger=logger) == \
		       PushResult(applied=False, rejected=0)
		conn.send_config_set.assert_not_called()     # nothing was sent
		assert "wrong shell" in log_of(logger)


def test_gaia_saves_with_its_own_command():
	"""Gaia leaves config mode, then saves with `save config`."""
	conn = connection(prompt="gw-1>")
	assert push("checkpoint_gaia", conn).applied
	assert finish_calls(conn) == [call.exit_config_mode(),
	                              call.send_command("save config")]


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


def test_rejected_commands_are_counted_and_the_rest_still_sent():
	"""A rejected command is counted (rejected=1) and the following ones are still sent."""
	conn = connection()
	conn.send_config_set.side_effect = ["% Invalid input detected", "ok", "ok"]
	result = push("cisco_ios", conn, commands=("bad", "good 1", "good 2"))
	assert result == PushResult(applied=True, rejected=1)
	assert conn.send_config_set.call_count == 3


# ── The device status ────────────────────────────────────────────────────────

def run(conn, commands, verify, config=None):
	"""A whole rollout to one IOS device over `conn`, the fetch returning `config` (or
	raising it): the device's result."""
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	conn.__enter__.return_value = conn            # the config fetch
	if isinstance(config, Exception):
		conn.send_command.side_effect = config
	else:
		conn.send_command.return_value = config or ""
	engine = RolloutEngine(RolloutOptions(verify=verify), [device], commands)
	with patch("netmiko.ConnectHandler", return_value=conn):
		(result,) = engine.run(threading.Event(),
		                       RolloutLogger(webapp=False, verbose=False))
	return result


@pytest.mark.parametrize("replies, status", [
	(["ok", "ok"], "success"),
	(["% Invalid input", "ok"], "partial"),
	(["% Invalid input", "% Invalid input"], "failed"),
])
def test_without_verify_the_device_replies_decide(replies, status):
	"""Without verify, the replies decide: none rejected is success, some partial,
	all failed."""
	conn = connection()
	conn.send_config_set.side_effect = replies
	assert run(conn, ["a", "b"], verify=False)["status"] == status


def test_operational_commands_dont_make_a_verified_device_partial():
	"""A command verify can't check (`write memory`) leaves the device a success and is
	counted among the verified commands."""
	result = run(connection(), ["hostname r1", "write memory"], verify=True,
	             config="hostname r1\n")
	assert result["status"] == "success"
	assert result["commands_verified"] == 2        # 1 verified + 1 not checkable


def test_a_config_that_cant_be_fetched_isnt_a_failure():
	"""When the config can't be fetched, the status comes from the push (success) and
	commands_verified is None."""
	result = run(connection(), ["hostname r1"], verify=True,
	             config=OSError("fetch timed out"))
	assert result["status"] == "success"            # from the push
	assert result["commands_verified"] is None


@pytest.mark.parametrize("config, status, verified", [
	("hostname r1\nntp server 1.1.1.1\n", "success", 2),
	("hostname r1\n", "partial", 1),
	("hostname other\n", "failed", 0),
])
def test_with_verify_the_config_decides(config, status, verified):
	"""With verify, the fetched config decides: both commands there is success, one
	partial, none failed, with the verified count."""
	result = run(connection(), ["hostname r1", "ntp server 1.1.1.1"],
	             verify=True, config=config)
	assert (result["status"], result["commands_verified"]) == (status, verified)


def test_failed_devices_are_not_verified():
	"""A device whose push failed (a failed Junos commit) is failed and its config is
	never fetched."""
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="juniper_junos", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(verify=True), [device], ["set x"])
	with patch("netmiko.ConnectHandler", return_value=conn), \
			patch.object(Device, "fetch_config") as fetch:
		(result,) = engine.run(threading.Event(),
		                       RolloutLogger(webapp=False, verbose=False))
	assert result["status"] == "failed"
	fetch.assert_not_called()


# ── Review fixes ─────────────────────────────────────────────────────────────

def test_a_one_word_command_is_still_seen_as_rejected():
	"""A complaint ending with a one-word command is a rejection; the same command echoed
	after a prompt isn't - only its prompt part is the echo."""
	assert rejection("Invalid input: foo", "foo")
	assert rejection("r1(config)#foo\nr1(config)#", "foo") is None


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


def test_all_real_commands_rejected_is_failed_even_with_navigation():
	"""Every real command rejected is failed, even though a navigation command (`exit`)
	was accepted."""
	conn = connection()
	conn.send_config_set.side_effect = ["% Invalid input", "% Invalid input", "ok"]
	result = run(conn, ["bad 1", "bad 2", "exit"], verify=False)
	assert result["status"] == "failed"


# ── The whole flow, per platform ─────────────────────────────────────────────

@pytest.mark.parametrize("device_type, config, expected", CASES,
                         ids=[c[0] for c in CASES])
def test_the_whole_rollout_on_each_platform(device_type, config, expected):
	"""push → the platform's finish → fetch with its show command(s) → the
	verdicts → the device status, with Netmiko mocked end to end: the status and
	verified count follow the verdicts, both sessions use the device's port, the
	platform's show commands are sent, and it commits or saves as its row says."""
	commands = [command for command, _ in expected]
	conn = connection(prompt={"fortinet": "fw1 #",
	                          "checkpoint_gaia": "gw-1>"}.get(device_type, "r1#"))
	conn.__enter__.return_value = conn
	conn.send_command.return_value = config
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type=device_type, secret="", port=2222)
	engine = RolloutEngine(RolloutOptions(verify=True), [device], commands)
	with patch("netmiko.ConnectHandler", return_value=conn) as handler:
		(result,) = engine.run(threading.Event(),
		                       RolloutLogger(webapp=False, verbose=False))

	verdicts = [v for _, v in expected]
	checkable = sum(v in (OK, MISSING, STILL) for v in verdicts)
	verified = verdicts.count(OK)
	assert result["status"] == ("success" if verified == checkable else
	                            "failed" if verified == 0 else "partial")
	assert result["commands_verified"] == verified + len(commands) - checkable
	# both sessions used the device's own port
	assert {c.kwargs["port"] for c in handler.call_args_list} == {2222}
	platform = PLATFORMS[device_type]
	shown = [c.args[0] for c in conn.send_command.call_args_list
	         if c.args and c.args[0] in platform.show_config]
	assert shown == list(platform.show_config)
	assert conn.commit.called == (platform.finish == "commit")
	assert conn.save_config.called == (platform.finish == "save")


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
	assert push("juniper_junos", conn, logger=logger) == \
	       PushResult(applied=False, rejected=0)
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
	assert "ACTION NEEDED — 10.0.0.1:22: the change is live but NOT saved" in log_of(logger)


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
	verdicts = verify_commands("cisco_xr", config, [
		"route-policy PASS", "pass", "end-policy",
		"router bgp 65000", "neighbor 10.0.0.2", "remote-as 65001"])
	assert verdicts == [OK, OK, NV, OK, OK, OK]



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
		config = device.fetch_config(fresh_logger())
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
	assert "saved from a new session" in log_of(logger)


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


def test_a_rejection_before_a_mid_list_timeout_still_counts():
	"""A command refused before the device stopped answering stays counted and
	logged; one accepted command keeps the device "partial"."""
	commands = ["ntp server bogus", "logging host 1.1.1.1", "crypto key generate rsa",
	            "snmp-server community x RO"]
	result, _, _, logger = stops_answering(
		["% Invalid input detected at '^' marker.", "ok",
		 netmiko.exceptions.ReadTimeout("no prompt")], commands)
	assert result["status"] == "partial" and result["commands_sent"] == 3
	assert "'ntp server bogus' rejected" in log_of(logger)


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


def test_a_new_hostname_after_a_rejection_keeps_the_rejection():
	"""The last command's timeout (a new hostname) is still saved from a new session -
	but a command refused before it keeps the device "partial" (it was reset to none:
	"success")."""
	result, _, handler, _ = stops_answering(
		["% Invalid input detected at '^' marker.", netmiko.exceptions.ReadTimeout("prompt")],
		["ntp server bogus", "hostname r9"])
	assert handler.call_count == 2                        # saved from a new session
	assert result["status"] == "partial"

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
	 "discard them on the device"),
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
	flagged = [l for l in log.splitlines() if "ACTION NEEDED — 10.0.0.1:22:" in l]
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
	shows), and the device is still a success: applied, saving is the issue."""
	conn = connection(prompt="gw-1>")
	conn.send_command.return_value = "CLINFR0771  Config lock is owned by admin."
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="checkpoint_gaia", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["set hostname x"])
	with patch("netmiko.ConnectHandler", return_value=conn):
		(result,) = engine.run(threading.Event(), fresh_logger())
	assert result["action_needed"].startswith("the change is live but NOT saved")
	assert result["status"] == "success"            # applied; saving is the issue


def test_no_instruction_in_a_clean_result():
	"""A clean device result has action_needed None."""
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type="cisco_ios", secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], ["hostname r1"])
	with patch("netmiko.ConnectHandler", return_value=connection()):
		(result,) = engine.run(threading.Event(), fresh_logger())
	assert result["action_needed"] is None
