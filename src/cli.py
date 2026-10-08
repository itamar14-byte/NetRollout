"""NetRollout's headless CLI (also the standalone netrollout-cli.exe): one
rollout from a devices CSV and a commands file - no database, no web app.

  python -m src.cli -d devices.csv -c commands.txt [-vf] [-v]

Missing paths are asked for; a prompted run also asks about Verify and
confirms before the push. The exit code says how it went (exit_code)."""
import argparse
import os
import sys
import threading
from argparse import ArgumentParser
from csv import DictReader
from typing import NoReturn

from src import runtime
from src.rollout.engine import DeviceResultDict, RolloutEngine, RolloutOptions
from src.rollout.inputs import InputParser, Validator
from src.rollout.log import RolloutLogger, prune_logs, utf8_console


def get_args() -> argparse.Namespace:
	""":returns: the command line's options (--help, --version exit here)"""
	parser = ArgumentParser(
		description="NetRollout — push configuration snippets to multiple network devices."
	)
	parser.add_argument("-d", "--devices",
	                    help="Path to a CSV file. Required fields: ip, device_type, port, username, password, secret")
	parser.add_argument("-c", "--commands",
	                    help="Path to a txt file containing commands to push, one per line")
	parser.add_argument("-vf", "--verify",
	                    help="Verify the configuration was applied: after the push, each device's config is read and every command checked",
	                    action="store_true")
	parser.add_argument("-v", "--verbose",
	                    help="Print logs to console",
	                    action="store_true")
	parser.add_argument("--version", action="version",
	                    version=f"NetRollout CLI {runtime.VERSION}")
	return parser.parse_args()


def main() -> NoReturn:
	"""One rollout, start to finish; exits with exit_code's code (130 on
	Ctrl+C, 2 when the input ends at a question)."""
	args = get_args()
	try:
		rollout(args)
	except KeyboardInterrupt:          # at a question, before the push
		print("\nInterrupted - nothing was pushed.")
		sys.exit(130)
	except EOFError:                   # stdin closed or piped out at a question
		print("\nNo answer (the input ended) - nothing was pushed.")
		sys.exit(2)


def rollout(args: argparse.Namespace) -> NoReturn:
	"""The questions, the checks, the push and the exit (main's body).

	:param args: get_args()'s"""
	# Anything asked for means a person is at the keyboard: they also get the
	# verify question and a last confirmation before the push
	interactive = not (args.devices and args.commands)
	devices_path  = args.devices  or ask_path("Enter device file path: ")
	commands_path = args.commands or ask_path("Enter commands file path: ")

	# -vf turns Verify on; a run with its paths given and no -vf is a script's:
	# off, never asked
	if args.verify:
		verify = True
	elif not interactive:
		verify = False
	else:
		verify = input("Verify rollout? (y/n): ").lower() == "y"

	options = RolloutOptions(verify=verify, verbose=args.verbose, webapp=False)
	logger  = RolloutLogger(webapp=False, verbose=args.verbose,
	                        prefix="cli_rollout")


	validator = Validator(logger)
	parser    = InputParser(validator, logger)

	# Read raw CSV rows and build Device objects directly — no DB, no user
	devices_path = devices_path.strip('"')
	try:
		with open(devices_path, "r", encoding="utf-8-sig") as f:
			raw_devices = list(DictReader(f))
	except FileNotFoundError:
		abort(logger, f"File not found: {devices_path}")
	except Exception as e:
		abort(logger, f"Failed to read device file: {e}")

	devices, errors  = parser.prepare_devices(raw_devices)
	for msg in errors:
		logger.notify(msg, "red")

	commands = parser.parse_commands(commands_path)
	if not devices or not commands:
		abort(logger, "Aborting — no devices or commands to process.")

	logger.notify("Verify: on" if verify else
	              "Verify: off (add -vf to check the config after the push)",
	              important=True)
	if interactive:
		answer = input(f"About to push {len(commands)} command"
		               f"{'s' if len(commands) != 1 else ''} to {len(devices)} "
		               f"device{'s' if len(devices) != 1 else ''}. Continue? (y/n): ")
		if answer.strip().lower() != "y":
			logger.notify("Cancelled — nothing was pushed.", "red")
			sys.exit(2)

	cancel = threading.Event()
	engine = RolloutEngine(param=options, devices=devices, commands=commands)

	# Ctrl+C during the push: the engine skips the devices not reached yet,
	# lets those being configured finish and returns (the summary is logged)
	try:
		results = engine.run(cancel, logger)
	except KeyboardInterrupt:
		# a second Ctrl+C, or one during verify: leave now, without waiting
		# for the devices still in flight
		cancel.set()
		logger.notify("Interrupted by user. Exiting.", "red")
		hard_exit(130)  # shell convention for Ctrl+C (128 + SIGINT)
	if cancel.is_set():
		logger.notify("Interrupted by user - the summary above shows what was done.", "red")
		sys.exit(130)
	try:
		pause()
	except KeyboardInterrupt:
		pass                           # the rollout is over: its result stands
	sys.exit(exit_code(results))


def hard_exit(code: int) -> NoReturn:
	"""Leave at once - without waiting for the push's threads still in
	flight (a second Ctrl+C means now)."""
	sys.stdout.flush()
	os._exit(code)


def ask_path(prompt: str) -> str:
	"""Ask until the file exists: a typo shouldn't end an interactive run.
	Paths dragged into a Windows terminal arrive wrapped in quotes.

	:returns: an existing file's path"""
	while True:
		path = input(prompt).strip().strip('"')
		if os.path.isfile(path):
			return path
		print(f"File not found: {path or '(nothing entered)'} — try again.")


def pause() -> None:
	"""Keep a double-clicked window open until the user has read it."""
	try:
		input("Press Enter to exit...")
	except EOFError:
		pass  # no terminal (cron, CI, piped stdin): nothing to wait for


def abort(logger: RolloutLogger, message: str) -> NoReturn:
	"""Stop before the push: nothing was applied anywhere, so exit 2."""
	logger.notify(message, "red")
	pause()
	sys.exit(2)


def exit_code(results: list[DeviceResultDict]) -> int:
	"""How the run went, for scripts and CI.

	:param results: one per device
	:returns: 0 = every device succeeded; 1 = mixed (some partial, failed or
	 cancelled); 2 = nothing applied anywhere (every device failed or was
	 cancelled - or the run stopped before the push: abort())"""
	statuses = [r["status"] for r in results]
	if statuses and all(s == "success" for s in statuses):
		return 0
	return 1 if any(s in ("success", "partial") for s in statuses) else 2


if __name__ == "__main__":
	utf8_console()
	prune_logs()  # CLI-only installs clean up too
	main()
