from argparse import ArgumentParser
from sys import exit
from time import sleep

from Core import parse_files, rollout_runner
from logging_utils import notify


def get_args():
    """Creates arguments for the headless CLI tool"""

    parser = ArgumentParser(
        description="A Network Automation tool to roll out configuration snippets on a"
        "set of devices."
    )
    parser.add_argument(
        "-d",
        "--devices",
        help="Path to a csv file. Required fields are ip, platform,"
        " username, password, secret and SSH port",
    )
    parser.add_argument(
        "-c",
        "--commands",
        help="Path to a txt file. Must contain the requested commands," " in order",
    )
    parser.add_argument(
        "-vy",
        "--verify",
        help="Test configuration file to verify successful running",
        action="store_const",
        const=True,
        default=None,
    )
    parser.add_argument(
        "-vb", "--verbose", help="Prints logs to console", action="store_true"
    )

    return parser.parse_args()


def main():
    # Gets the parameters from file paths and boolean flag status. If no input was entered through cli,
    # user will be prompted to enter the data
    args = get_args()
    devices_path = args.devices or input("Enter the device file path: ")
    commands_path = args.commands or input("Enter the commands file path: ")

    # If the verify flag was supplied, we activate verification, and if other
    # flags were supplied we disable it,
    # and if no flags were supplied we prompt for verification alongside the other flag prompts
    if args.verify is True:
        verify_rollout = True
    elif args.devices and args.commands:
        verify_rollout = False
    else:
        verify_rollout = (
            True
            if input("Do you want to verify roll out? (y/n): ").lower() == "y"
            else False
        )
    verbose = args.verbose
    devices, commands = parse_files(devices_path, commands_path, verbose)

    exit_code = rollout_runner(
	    devices=devices,
	    commands=commands,
	    verify_rollout=verify_rollout,
	    verbose=verbose
    )
    sleep(10)
    exit(exit_code)



if __name__ == "__main__":
    # Runs the main function that executes the tool. In Ctrl+C from the user, the system exits
    try:
        main()
    except KeyboardInterrupt:
        notify("Interrupted by User. Exiting Program")
        exit(0)
