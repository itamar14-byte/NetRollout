import sys
import threading
from argparse import ArgumentParser
from csv import DictReader

from core import RolloutOptions, RolloutEngine
from input_parser import InputParser
from logging_utils import RolloutLogger
from validation import Validator


def get_args():
	"""Creates arguments for the headless CLI tool"""
	parser = ArgumentParser(
		description="NetRollout — push configuration snippets to multiple network devices."
	)
	parser.add_argument("-d", "--devices",
	                    help="Path to a CSV file. Required fields: ip, device_type, port, username, password, secret")
	parser.add_argument("-c", "--commands",
	                    help="Path to a txt file containing commands to push, one per line")
	parser.add_argument("-vy", "--verify",
	                    help="Verify configuration was applied after push (uses NAPALM)",
	                    action="store_true")
	parser.add_argument("-vb", "--verbose",
	                    help="Print logs to console",
	                    action="store_true")
	return parser.parse_args()


def main():
	# Gets the parameters from file paths and boolean flag status. If no input was entered through cli,
	# user will be prompted to enter the data
	args = get_args()
	devices_path  = args.devices  or input("Enter device file path: ")
	commands_path = args.commands or input("Enter commands file path: ")

	# If the verify flag was supplied, we activate verification, and if other
	# flags were supplied we disable it,
	# and if no flags were supplied we prompt for verification alongside the other flag prompts
	if args.verify:
		verify = True
	elif args.devices and args.commands:
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
		logger.notify(f"File not found: {devices_path}", "red")
		sys.exit(1)
	except Exception as e:
		logger.notify(f"Failed to read device file: {e}", "red")
		sys.exit(1)

	devices, errors  = parser.prepare_devices(raw_devices)
	for msg in errors:
		logger.notify(msg, "red")

	commands = parser.parse_commands(commands_path)
	if not devices or not commands:
		logger.notify("Aborting — no devices or commands to process.", "red")
		sys.exit(1)

	cancel = threading.Event()
	engine = RolloutEngine(param=options, devices=devices, commands=commands)

	# Runs the main function that executes the tool.
	# On Ctrl+C from the user, the cancel event is set and the system exits
	try:
		engine.run(cancel, logger)
		input("Press Enter to exit...")
		sys.exit(0)
	except KeyboardInterrupt:
		cancel.set()
		logger.notify("Interrupted by user. Exiting.", "red")
		sys.exit(0)


if __name__ == "__main__":
	main()
