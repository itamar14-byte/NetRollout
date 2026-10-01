"""Stage 4b: every supported platform — how the push finishes, how a refused
command is recognised, and the verify verdicts against the config as each
platform prints it (fixtures approximate real output; the EVE-NG round is
the real-device check)."""
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from src.core import (COMMIT_TIMEOUT, NOT_CONFIGURED, PLATFORMS, STILL_CONFIGURED,
                      UNVERIFIABLE, VARIABLE, VERIFIED, Device, PushResult,
                      RolloutEngine, RolloutOptions, rejection, verify_commands)
from src.logging_utils import RolloutLogger
from src.validation import Validator

OK, MISSING, STILL, NV = VERIFIED, NOT_CONFIGURED, STILL_CONFIGURED, UNVERIFIABLE


def test_every_supported_platform_has_a_row():
	assert set(PLATFORMS) == set(Validator.SUPPORTED_PLATFORMS)


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
		("root", MISSING),                              # XR's "root": not a navigation word we know
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
	commands = [command for command, _ in expected]
	got = verify_commands(device_type, config, commands)
	assert list(zip(commands, got)) == expected


def test_unresolved_variables_are_not_judged():
	verdicts = verify_commands("cisco_ios", IOS_CONFIG,
	                           ["hostname $$HOSTNAME$$", "hostname r1"])
	assert verdicts == [VARIABLE, OK]


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
])
def test_rejections_are_recognised(command, output):
	assert rejection(output, command)


@pytest.mark.parametrize("command, output", [
	("description unknown-host", "r1(config-if)#description unknown-host\nr1(config-if)#"),
	("set comments invalid-input-filter", "fw1 (port1) # set comments invalid-input-filter\nfw1 (port1) #"),
	("hostname r2", "r2(config)#"),
])
def test_an_accepted_command_echo_is_not_a_rejection(command, output):
	assert rejection(output, command) is None


# ── How the push finishes, per platform ──────────────────────────────────────

def push(device_type, conn, commands=("hostname x",), logger=None):
	device = Device(ip="10.0.0.1", label="d", username="u", password="p",
	                device_type=device_type, secret="", port=22)
	engine = RolloutEngine(RolloutOptions(), [device], list(commands))
	with patch("netmiko.ConnectHandler", return_value=conn):
		_, results = engine._push_config(
			threading.Event(),
			logger or RolloutLogger(webapp=False, verbose=False))
	return results[0]


def connection(prompt="r1#"):
	conn = MagicMock()
	conn.send_config_set.return_value = "ok"
	conn.find_prompt.return_value = prompt
	return conn


def finish_calls(conn):
	return [c for c in conn.method_calls
	        if c[0] in ("commit", "exit_config_mode", "save_config",
	                    "send_command", "send_config_set", "send_command_timing")
	        and c != call.send_config_set(["hostname x"], exit_config_mode=False)]


@pytest.mark.parametrize("device_type", [t for t, p in PLATFORMS.items()
                                         if p.finish == "save"])
def test_save_platforms_leave_config_mode_then_save(device_type):
	conn = connection()
	assert push(device_type, conn) == PushResult(applied=True, rejected=0)
	assert finish_calls(conn) == [call.exit_config_mode(), call.save_config()]


@pytest.mark.parametrize("device_type", ["juniper_junos", "paloalto_panos",
                                         "cisco_xr"])
def test_commit_platforms_commit_before_leaving_config_mode(device_type):
	conn = connection()
	assert push(device_type, conn).applied
	assert finish_calls(conn) == [call.commit(read_timeout=COMMIT_TIMEOUT), call.exit_config_mode()]


def test_junos_failed_commit_rolls_back_and_reports_not_applied():
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed: error: x")
	result = push("juniper_junos", conn)
	assert result == PushResult(applied=False, rejected=0)
	assert finish_calls(conn) == [
		call.commit(read_timeout=COMMIT_TIMEOUT),
		call.send_config_set(["rollback 0"], exit_config_mode=False),
		call.exit_config_mode()]


def test_panos_failed_commit_leaves_the_candidate_and_says_so():
	conn = connection()
	conn.commit.side_effect = ValueError("Commit failed")
	logger = RolloutLogger(webapp=False, verbose=False)
	assert not push("paloalto_panos", conn, logger=logger).applied
	assert call.send_config_set(["rollback 0"], exit_config_mode=False) \
	       not in conn.method_calls
	with open(logger.logfile, encoding="utf-8") as f:   # the rollout log
		assert "still in the candidate configuration" in f.read()


def test_gaia_saves_with_its_own_command():
	conn = connection()
	assert push("checkpoint_gaia", conn).applied
	assert finish_calls(conn) == [call.exit_config_mode(),
	                              call.send_command("save config")]


def test_fortios_closes_open_blocks_and_saves_nothing():
	conn = connection()
	conn.find_prompt.side_effect = ["fw1 (ipv6) #", "fw1 (port1) #",
	                                "fw1 (interface) #", "fw1 #"]
	assert push("fortinet", conn, commands=("config system interface",)).applied
	assert conn.send_command_timing.call_args_list == [call("end")] * 3
	conn.save_config.assert_not_called()
	conn.commit.assert_not_called()


def test_rejected_commands_are_counted_and_the_rest_still_sent():
	conn = connection()
	conn.send_config_set.side_effect = ["% Invalid input detected", "ok", "ok"]
	result = push("cisco_ios", conn, commands=("bad", "good 1", "good 2"))
	assert result == PushResult(applied=True, rejected=1)
	assert conn.send_config_set.call_count == 3


# ── The device status ────────────────────────────────────────────────────────

def run(conn, commands, verify, config=None):
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
	conn = connection()
	conn.send_config_set.side_effect = replies
	assert run(conn, ["a", "b"], verify=False)["status"] == status


def test_operational_commands_dont_make_a_verified_device_partial():
	result = run(connection(), ["hostname r1", "write memory"], verify=True,
	             config="hostname r1\n")
	assert result["status"] == "success"
	assert result["commands_verified"] == 2        # 1 verified + 1 not checkable


def test_a_config_that_cant_be_fetched_isnt_a_failure():
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
	result = run(connection(), ["hostname r1", "ntp server 1.1.1.1"],
	             verify=True, config=config)
	assert (result["status"], result["commands_verified"]) == (status, verified)


def test_failed_devices_are_not_verified():
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
	# the complaint ends with the command — only its prompt part is the echo
	assert rejection("Invalid input: foo", "foo")
	assert rejection("r1(config)#foo\nr1(config)#", "foo") is None


def test_a_commit_that_outlasts_the_wait_is_reported_honestly():
	import netmiko
	conn = connection()
	conn.commit.side_effect = netmiko.exceptions.ReadTimeout("slow")
	logger = RolloutLogger(webapp=False, verbose=False)
	assert not push("paloalto_panos", conn, logger=logger).applied
	with open(logger.logfile, encoding="utf-8") as f:
		assert "didn't finish within" in f.read()
	conn.disconnect.assert_called_once()


def test_the_session_is_closed_when_the_push_fails_half_way():
	conn = connection()
	conn.send_config_set.side_effect = OSError("socket closed")
	assert not push("cisco_ios", conn).applied
	conn.disconnect.assert_called_once()


def test_all_real_commands_rejected_is_failed_even_with_navigation():
	conn = connection()
	conn.send_config_set.side_effect = ["% Invalid input", "% Invalid input", "ok"]
	result = run(conn, ["bad 1", "bad 2", "exit"], verify=False)
	assert result["status"] == "failed"


# ── The whole flow, per platform ─────────────────────────────────────────────

@pytest.mark.parametrize("device_type, config, expected", CASES,
                         ids=[c[0] for c in CASES])
def test_the_whole_rollout_on_each_platform(device_type, config, expected):
	"""push → the platform's finish → fetch with its show command(s) → the
	verdicts → the device status, with Netmiko mocked end to end."""
	commands = [command for command, _ in expected]
	conn = connection(prompt="fw1 #" if device_type == "fortinet" else "r1#")
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
