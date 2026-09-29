"""Headless CLI (src/cli.py): argument parsing, prompts, file input, and the
hand-off to RolloutEngine. The engine and TCP probe are mocked — no devices,
no network."""
import sys
from unittest.mock import MagicMock, patch

import pytest

from src import cli

CSV_HEADER = "ip,username,password,device_type,secret,port\n"
GOOD_ROW = "10.0.0.1,admin,pw,cisco_ios,en,22\n"


@pytest.fixture
def files(tmp_path):
	"""Write a devices CSV and a commands file; returns their paths."""
	def _write(rows=GOOD_ROW, commands="hostname r1\nntp server 1.1.1.1\n",
	           commands_name="commands.txt", commands_bytes=None):
		devices = tmp_path / "devices.csv"
		devices.write_text(CSV_HEADER + rows, encoding="utf-8")
		cmds = tmp_path / commands_name
		if commands_bytes is not None:
			cmds.write_bytes(commands_bytes)
		else:
			cmds.write_text(commands, encoding="utf-8")
		return str(devices), str(cmds)
	return _write


@pytest.fixture
def run_cli(monkeypatch):
	"""Run cli.main() with argv and scripted input() answers. Returns
	(exit_code, engine_ctor, prompts) — engine_ctor is the mocked
	RolloutEngine class (None calls when no rollout started). The engine
	reports every device as `statuses` (default: all success)."""
	def _run(argv, answers=(), reachable=True, engine_run=None,
	         statuses=("success",)):
		monkeypatch.setattr(sys, "argv", ["cli.py", *argv])
		prompts, queue = [], list(answers)

		def fake_input(prompt=""):
			prompts.append(prompt)
			return queue.pop(0) if queue else ""
		monkeypatch.setattr("builtins.input", fake_input)
		engine_ctor = MagicMock(name="RolloutEngine")
		engine_ctor.return_value.run.return_value = results(*statuses)
		if engine_run is not None:
			engine_ctor.return_value.run.side_effect = engine_run
		with patch.object(cli, "RolloutEngine", engine_ctor), \
				patch("src.validation.Validator.test_tcp_port",
				      return_value=reachable):
			with pytest.raises(SystemExit) as exc:
				cli.main()
		return exc.value.code, engine_ctor, prompts
	return _run


def engine_args(engine_ctor):
	return engine_ctor.call_args.kwargs


def results(*statuses):
	"""Minimal engine results: only `status` matters to the CLI."""
	return [{"status": s} for s in statuses]


# ── Arguments ────────────────────────────────────────────────────────────────

def test_get_args_parses_short_and_long_flags(monkeypatch):
	monkeypatch.setattr(sys, "argv", ["cli.py", "-d", "d.csv", "--commands",
	                                  "c.txt", "-vy", "--verbose"])
	args = cli.get_args()
	assert (args.devices, args.commands, args.verify, args.verbose) == \
	       ("d.csv", "c.txt", True, True)


def test_get_args_defaults(monkeypatch):
	monkeypatch.setattr(sys, "argv", ["cli.py"])
	args = cli.get_args()
	assert (args.devices, args.commands, args.verify, args.verbose) == \
	       (None, None, False, False)


# ── Happy path ───────────────────────────────────────────────────────────────

def test_rollout_runs_with_parsed_devices_and_commands(files, run_cli):
	devices, commands = files()
	code, engine, prompts = run_cli(["-d", devices, "-c", commands])
	assert code == 0
	kw = engine_args(engine)
	(device,) = kw["devices"]
	assert (device.ip, device.port, device.username, device.device_type,
	        device.label) == ("10.0.0.1", 22, "admin", "cisco_ios", "10.0.0.1")
	assert [c.strip() for c in kw["commands"]] == ["hostname r1",
	                                               "ntp server 1.1.1.1"]
	# both paths given and no -vy: verify off, no prompts before the run
	assert kw["param"].verify is False and kw["param"].webapp is False
	assert prompts == ["Press Enter to exit..."]
	engine.return_value.run.assert_called_once()


def test_verify_and_verbose_flags_reach_the_engine(files, run_cli):
	devices, commands = files()
	_, engine, _ = run_cli(["-d", devices, "-c", commands, "-vy", "-vb"])
	param = engine_args(engine)["param"]
	assert param.verify is True and param.verbose is True


# ── Interactive prompts ──────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,expected", [("y", True), ("Y", True),
                                             ("n", False), ("", False)])
def test_prompts_for_missing_paths_and_verify(files, run_cli, answer, expected):
	devices, commands = files()
	# paths dragged into a Windows terminal arrive wrapped in quotes
	code, engine, prompts = run_cli(
		[], answers=[f'"{devices}"', f'"{commands}"', answer])
	assert code == 0
	assert prompts[:3] == ["Enter device file path: ",
	                       "Enter commands file path: ",
	                       "Verify rollout? (y/n): "]
	assert engine_args(engine)["param"].verify is expected


def test_only_devices_given_prompts_for_commands_and_verify(files, run_cli):
	devices, commands = files()
	_, engine, prompts = run_cli(["-d", devices], answers=[commands, "y"])
	assert prompts[:2] == ["Enter commands file path: ",
	                       "Verify rollout? (y/n): "]
	assert engine_args(engine)["param"].verify is True


# ── Input errors: abort before any rollout ───────────────────────────────────

def test_missing_devices_file_exits_1(files, run_cli, tmp_path):
	_, commands = files()
	code, engine, _ = run_cli(["-d", str(tmp_path / "nope.csv"),
	                           "-c", commands])
	assert code == 1 and not engine.called


def test_wrong_commands_extension_exits_1(files, run_cli):
	devices, commands = files(commands_name="commands.cfg")
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 1 and not engine.called


def test_bad_rows_are_skipped_and_good_rows_still_run(files, run_cli):
	rows = ("999.0.0.1,admin,pw,cisco_ios,,22\n"       # bad ip
	        "10.0.0.2,admin,pw,not_a_platform,,22\n"    # bad platform
	        "10.0.0.3,,,cisco_ios,,22\n"                # no credentials
	        "10.0.0.4,admin,pw,Cisco_IOS,,2222\n")      # ok (type is lowercased)
	devices, commands = files(rows=rows)
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 0
	(device,) = engine_args(engine)["devices"]
	assert (device.ip, device.port, device.device_type) == \
	       ("10.0.0.4", 2222, "cisco_ios")


def test_no_valid_devices_exits_1(files, run_cli):
	devices, commands = files(rows="999.0.0.1,admin,pw,cisco_ios,,22\n")
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 1 and not engine.called


def test_unreachable_devices_are_dropped(files, run_cli):
	devices, commands = files()
	code, engine, _ = run_cli(["-d", devices, "-c", commands],
	                          reachable=False)
	assert code == 1 and not engine.called


def test_empty_commands_file_exits_1(files, run_cli):
	devices, commands = files(commands="")
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 1 and not engine.called


# ── Exit code reflects the outcome ───────────────────────────────────────────

@pytest.mark.parametrize("statuses,expected", [
	(("success", "success"), 0),
	(("success", "failed"), 1),
	(("success", "cancelled"), 1),
	(("partial", "failed"), 1),    # partial = some commands applied
	(("failed", "failed"), 2),
	(("failed", "cancelled"), 2),
	((), 2),
])
def test_exit_code_reflects_device_outcomes(files, run_cli, statuses,
                                            expected):
	devices, commands = files()
	code, _, _ = run_cli(["-d", devices, "-c", commands], statuses=statuses)
	assert code == expected


# ── Ctrl+C ───────────────────────────────────────────────────────────────────

def test_ctrl_c_sets_cancel_and_exits_130(files, run_cli):
	devices, commands = files()
	seen = {}

	def interrupted(cancel, logger):
		seen["cancel"] = cancel
		raise KeyboardInterrupt
	code, _, prompts = run_cli(["-d", devices, "-c", commands],
	                           engine_run=interrupted)
	assert code == 130  # shell convention for Ctrl+C
	assert seen["cancel"].is_set()
	assert "Press Enter to exit..." not in prompts


# ── Commands file: same rules as the web path ───────────────────────────────

def test_blank_command_lines_are_ignored(files, run_cli):
	devices, commands = files(commands="hostname r1\n\n   \nntp server 1.1.1.1\n")
	_, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert engine_args(engine)["commands"] == ["hostname r1",
	                                           "ntp server 1.1.1.1"]


def test_utf8_bom_commands_file(files, run_cli):
	# Notepad's "UTF-8 with BOM" used to glue U+FEFF to the first command
	devices, commands = files(
		commands_bytes="hostname r1\nntp server 1.1.1.1\n".encode("utf-8-sig"))
	_, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert engine_args(engine)["commands"][0] == "hostname r1"


def test_non_utf8_commands_file_exits_1(files, run_cli):
	devices, commands = files(commands_bytes="description café\n".encode("cp1252"))
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 1 and not engine.called


# ── Non-interactive use ──────────────────────────────────────────────────────

def test_non_interactive_run_exits_cleanly(files, monkeypatch):
	# the final "Press Enter" prompt used to raise EOFError without a tty
	devices, commands = files()
	monkeypatch.setattr(sys, "argv", ["cli.py", "-d", devices, "-c", commands])

	def no_stdin(prompt=""):
		raise EOFError
	monkeypatch.setattr("builtins.input", no_stdin)
	engine = MagicMock()
	engine.return_value.run.return_value = results("success")
	with patch.object(cli, "RolloutEngine", engine), \
			patch("src.validation.Validator.test_tcp_port", return_value=True):
		with pytest.raises(SystemExit) as exc:
			cli.main()
	assert exc.value.code == 0
	engine.return_value.run.assert_called_once()
