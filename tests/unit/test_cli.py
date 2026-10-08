"""Headless CLI (src/cli.py): argument parsing, prompts, file input, and the
hand-off to RolloutEngine. The engine and TCP probe are mocked — no devices,
no network."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import cli, runtime

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
		def hard_exit(code):           # os._exit would end the test run
			raise SystemExit(code)
		with patch.object(cli, "RolloutEngine", engine_ctor), \
				patch.object(cli, "hard_exit", hard_exit), \
				patch("src.rollout.inputs.tcp_reachable",
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
	"""-d, --commands, -vf and --verbose are parsed into devices, commands,
	verify and verbose."""
	monkeypatch.setattr(sys, "argv", ["cli.py", "-d", "d.csv", "--commands",
	                                  "c.txt", "-vf", "--verbose"])
	args = cli.get_args()
	assert (args.devices, args.commands, args.verify, args.verbose) == \
	       ("d.csv", "c.txt", True, True)


def test_get_args_defaults(monkeypatch):
	"""With no flags: no paths, verify and verbose off."""
	monkeypatch.setattr(sys, "argv", ["cli.py"])
	args = cli.get_args()
	assert (args.devices, args.commands, args.verify, args.verbose) == \
	       (None, None, False, False)


# ── Happy path ───────────────────────────────────────────────────────────────

def test_rollout_runs_with_parsed_devices_and_commands(files, run_cli):
	"""A full-flag run hands the engine the parsed device and commands, verify
	off and not webapp, asks nothing but the closing pause, exits 0."""
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
	"""-vf and -v reach the engine as verify and verbose on."""
	devices, commands = files()
	_, engine, _ = run_cli(["-d", devices, "-c", commands, "-vf", "-v"])
	param = engine_args(engine)["param"]
	assert param.verify is True and param.verbose is True


# ── Interactive prompts ──────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,expected", [("y", True), ("Y", True),
                                             ("n", False), ("", False)])
def test_prompts_for_missing_paths_and_verify(files, run_cli, answer, expected):
	"""Without flags it asks for both paths (quotes stripped), verify and the
	confirmation; y/Y turn verify on, n or nothing leave it off."""
	devices, commands = files()
	# paths dragged into a Windows terminal arrive wrapped in quotes
	code, engine, prompts = run_cli(
		[], answers=[f'"{devices}"', f'"{commands}"', answer, "y"])
	assert code == 0
	assert prompts[:4] == ["Enter device file path: ",
	                       "Enter commands file path: ",
	                       "Verify rollout? (y/n): ",
	                       "About to push 2 commands to 1 device. Continue? (y/n): "]
	assert engine_args(engine)["param"].verify is expected


def test_only_devices_given_prompts_for_commands_and_verify(files, run_cli):
	"""With only -d it asks for the commands path and verify; "y" turns verify on."""
	devices, commands = files()
	_, engine, prompts = run_cli(["-d", devices], answers=[commands, "y", "y"])
	assert prompts[:2] == ["Enter commands file path: ",
	                       "Verify rollout? (y/n): "]
	assert engine_args(engine)["param"].verify is True


# ── Input errors: abort before any rollout ───────────────────────────────────

def test_missing_devices_file_exits_2(files, run_cli, tmp_path):
	"""A devices file that doesn't exist exits 2 without a rollout."""
	_, commands = files()
	code, engine, _ = run_cli(["-d", str(tmp_path / "nope.csv"),
	                           "-c", commands])
	assert code == 2 and not engine.called


def test_wrong_commands_extension_exits_2(files, run_cli):
	"""A commands file that isn't .txt exits 2 without a rollout."""
	devices, commands = files(commands_name="commands.cfg")
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 2 and not engine.called


def test_bad_rows_are_skipped_and_good_rows_still_run(files, run_cli):
	"""Rows with a bad IP, an unknown platform or no credentials are skipped; the
	good row runs, its device type lowercased, and the run exits 0."""
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


def test_no_valid_devices_exits_2(files, run_cli):
	"""No valid device row exits 2 without a rollout."""
	devices, commands = files(rows="999.0.0.1,admin,pw,cisco_ios,,22\n")
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 2 and not engine.called


def test_unreachable_devices_are_dropped(files, run_cli):
	"""Unreachable devices are dropped; none left exits 2 without a rollout,
	ending with the "Press Enter to exit" pause."""
	devices, commands = files()
	code, engine, prompts = run_cli(["-d", devices, "-c", commands],
	                                reachable=False)
	assert code == 2 and not engine.called
	# a double-clicked window stays open long enough to read why
	assert prompts[-1] == "Press Enter to exit..."


def test_empty_commands_file_exits_2(files, run_cli):
	"""An empty commands file exits 2 without a rollout."""
	devices, commands = files(commands="")
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 2 and not engine.called


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
	"""The exit code follows the devices' outcomes: 0 all succeeded, 1 mixed
	(success with failed or cancelled; partial with failed), 2 nothing
	applied (only failed / cancelled, or no results)."""
	devices, commands = files()
	code, _, _ = run_cli(["-d", devices, "-c", commands], statuses=statuses)
	assert code == expected


# ── Ctrl+C ───────────────────────────────────────────────────────────────────

def test_ctrl_c_sets_cancel_and_exits_130(files, run_cli):
	"""A Ctrl+C the engine doesn't take (a second one while the devices in
	flight finish, or during verify) sets the cancel flag and exits 130 at
	once, without the closing pause."""
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


def test_an_interrupted_rollout_exits_130_after_its_summary(files, run_cli):
	"""Ctrl+C during the push: the engine cancels (skips the devices not reached)
	and returns its results - the CLI exits 130, not as a finished rollout."""
	devices, commands = files()

	def cancelled(cancel, logger):
		cancel.set()                 # what the engine does on Ctrl+C
		return results("success", "cancelled")
	code, _, prompts = run_cli(["-d", devices, "-c", commands], engine_run=cancelled)
	assert code == 130
	assert "Press Enter to exit..." not in prompts


@pytest.mark.parametrize("error, code", [(KeyboardInterrupt, 130), (EOFError, 2)])
def test_ctrl_c_or_no_input_at_a_prompt_ends_cleanly(files, run_cli, monkeypatch,
                                                      error, code):
	"""Ctrl+C at a prompt exits 130, the input ending (a closed or piped stdin)
	exits 2 - nothing pushed, no traceback."""
	devices, commands = files()

	def failing_input(prompt=""):
		raise error
	monkeypatch.setattr("builtins.input", failing_input)
	monkeypatch.setattr(sys, "argv", ["cli.py", "-d", devices])     # asks for commands
	engine_ctor = MagicMock(name="RolloutEngine")
	with patch.object(cli, "RolloutEngine", engine_ctor), pytest.raises(SystemExit) as exc:
		cli.main()
	assert exc.value.code == code and not engine_ctor.return_value.run.called


def test_ctrl_c_at_the_closing_pause_keeps_the_result(files, run_cli, monkeypatch):
	"""Ctrl+C at "Press Enter to exit" after a finished rollout keeps the
	rollout's exit code (0) - it isn't an interrupted rollout."""
	devices, commands = files()
	monkeypatch.setattr(cli, "pause", lambda: (_ for _ in ()).throw(KeyboardInterrupt))
	code, _, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 0


# ── Commands file: same rules as the web path ───────────────────────────────

def test_blank_command_lines_are_ignored(files, run_cli):
	"""Empty and whitespace-only lines in the commands file are dropped."""
	devices, commands = files(commands="hostname r1\n\n   \nntp server 1.1.1.1\n")
	_, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert engine_args(engine)["commands"] == ["hostname r1",
	                                           "ntp server 1.1.1.1"]


def test_utf8_bom_commands_file(files, run_cli):
	"""Notepad's "UTF-8 with BOM" used to glue U+FEFF to the first command: the
	first command is read without it."""
	devices, commands = files(
		commands_bytes="hostname r1\nntp server 1.1.1.1\n".encode("utf-8-sig"))
	_, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert engine_args(engine)["commands"][0] == "hostname r1"


def test_non_utf8_commands_file_exits_2(files, run_cli):
	"""A commands file that isn't UTF-8 (cp1252) exits 2 without a rollout."""
	devices, commands = files(commands_bytes="description café\n".encode("cp1252"))
	code, engine, _ = run_cli(["-d", devices, "-c", commands])
	assert code == 2 and not engine.called


# ── Non-interactive use ──────────────────────────────────────────────────────

def test_non_interactive_run_exits_cleanly(files, monkeypatch):
	"""The final "Press Enter" prompt used to raise EOFError without a tty: with
	no stdin the run still pushes once and exits 0."""
	devices, commands = files()
	monkeypatch.setattr(sys, "argv", ["cli.py", "-d", devices, "-c", commands])

	def no_stdin(prompt=""):
		raise EOFError
	monkeypatch.setattr("builtins.input", no_stdin)
	engine = MagicMock()
	engine.return_value.run.return_value = results("success")
	with patch.object(cli, "RolloutEngine", engine), \
			patch("src.rollout.inputs.tcp_reachable", return_value=True):
		with pytest.raises(SystemExit) as exc:
			cli.main()
	assert exc.value.code == 0
	engine.return_value.run.assert_called_once()


# ── Stage 5: the CLI stands alone (it ships as a PyInstaller .exe) ──

WEB_STACK = {"flask", "sqlalchemy", "redis", "psycopg2", "alembic",
             "flask_login", "flask_session", "flask_wtf", "flask_limiter",
             "waitress", "prometheus_client", "prometheus_flask_exporter",
             "ldap3", "pyotp", "qrcode", "PIL", "werkzeug", "jinja2"}


def test_the_cli_loads_nothing_from_the_web_stack():
	"""Importing src.cli in a fresh interpreter loads no module of the web stack
	(WEB_STACK)."""
	# A fresh interpreter: this test session has the web app loaded already
	probe = ("import json, sys, src.cli; "
	         "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))")
	out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
	                     text=True, cwd=Path(__file__).resolve().parents[2],
	                     check=True).stdout
	assert not WEB_STACK & set(json.loads(out))


def test_version_flag(capsys, monkeypatch):
	"""--version prints "NetRollout CLI <version>" and exits 0."""
	monkeypatch.setattr(sys, "argv", ["netrollout-cli", "--version"])
	with pytest.raises(SystemExit) as exit_info:
		cli.get_args()
	assert exit_info.value.code == 0
	assert capsys.readouterr().out.strip() == f"NetRollout CLI {runtime.VERSION}"


# ── Interactive safety: retry a typo, confirm before the push ──

def test_a_mistyped_path_is_asked_again(files, run_cli, tmp_path):
	"""A path that doesn't exist, or nothing, is asked for again until a file
	is given; the run then goes ahead."""
	devices, commands = files()
	code, engine, prompts = run_cli(
		[], answers=[str(tmp_path / "typo.csv"), "", devices, commands, "n", "y"])
	assert code == 0 and engine.called
	assert prompts[:3] == ["Enter device file path: "] * 3


def test_declining_the_confirmation_pushes_nothing(files, run_cli):
	"""Answering "n" to "About to push…" exits 2 without a rollout."""
	devices, commands = files()
	code, engine, prompts = run_cli([], answers=[devices, commands, "n", "n"])
	assert code == 2 and not engine.called
	assert prompts[-1].startswith("About to push")


def test_full_flag_runs_never_ask(files, run_cli):
	"""Scripts: no confirmation; only the closing pause (skipped without a tty)."""
	devices, commands = files()
	_, engine, prompts = run_cli(["-d", devices, "-c", commands])
	assert engine.called and prompts == ["Press Enter to exit..."]


@pytest.mark.parametrize("argv,expected", [
	([], "Verify: off (add -vf to check the config after the push)"),
	(["-vf"], "Verify: on"),
])
def test_the_verify_choice_is_always_shown(files, run_cli, capsys, argv,
                                           expected):
	"""The verify choice is always printed: off (with the hint to add -vf) or on."""
	devices, commands = files()
	run_cli(["-d", devices, "-c", commands, *argv])
	assert expected in capsys.readouterr().out
