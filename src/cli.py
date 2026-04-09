import sys
import threading
from argparse import ArgumentParser
from core import parse_files, RolloutOptions, RolloutEngine
from logging_utils import base_notify


def get_args():
	"""Creates arguments for the headless CLI tool"""

	parser = ArgumentParser(
		description="A Network Automation tool to roll out configuration snippets on a"
		            "set of _devices."
	)
	parser.add_argument(
		"-d",
		"--_devices",
		help="Path to a csv file. Required fields are ip, platform,"
		     " username, password, secret and SSH port",
	)
	parser.add_argument(
		"-c",
		"--_commands",
		help="Path to a txt file. Must contain the requested _commands," " in order",
	)
	parser.add_argument(
		"-vy",
		"--_verify",
		help="Test configuration file to _verify successful running",
		action="store_true",
	)
	parser.add_argument(
		"-vb", "--_verbose", help="Prints logs to console", action="store_true"
	)

	return parser.parse_args()


def main():
	# Gets the parameters from file paths and boolean flag status. If no input was entered through cli,
	# user will be prompted to enter the data
	args = get_args()
	devices_path = args.devices or input("Enter the device file path: ")
	commands_path = args.commands or input("Enter the _commands file path: ")

	# If the _verify flag was supplied, we activate verification, and if other
	# flags were supplied we disable it,
	# and if no flags were supplied we prompt for verification alongside the other flag prompts
	if args.verify:
		verify_rollout = True
	elif args.devices and args.commands:
		verify_rollout = False
	else:
		verify_rollout = (
			True
			if input("Do you want to _verify roll out? (y/n): ").lower() == "y"
			else False
		)
	params = RolloutOptions(verbose=args.verbose,
	                        verify=verify_rollout,
	                        webapp=False)
	devices, commands = parse_files(devices_path, commands_path, params.verbose)
	cancel = threading.Event()
	run_instance = RolloutEngine(
		param=params,
		devices=devices,
		commands=commands,
		cancel_event=cancel
	)

	# Runs the main function that executes the tool.
	# In Ctrl+C from the user, the system exits
	try:
		exit_code = run_instance.run()
		input("Press Enter to continue...")
		sys.exit(exit_code)
	except KeyboardInterrupt:
		run_instance.cancel_event.set()
		base_notify("Interrupted by User. Exiting Program")
		sys.exit(0)


if __name__ == "__main__":
	main()
