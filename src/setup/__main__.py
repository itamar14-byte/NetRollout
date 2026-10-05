"""python -m src.setup init | check - see src/setup/__init__.py."""
import argparse
import sys

from src.setup import answers as A
from src.setup import files

OK, INVALID, REFUSED = 0, 1, 2
ANSWER_FLAGS = ("hostname", "https_port", "monitoring", "org_certificate",
                "timezone")


def parse_args(argv):
	p = argparse.ArgumentParser(prog="python -m src.setup")
	p.add_argument("command", choices=("init", "check"))
	facts = p.add_argument_group("facts (from the host script)")
	facts.add_argument("--os", choices=("windows", "linux"), default="linux")
	facts.add_argument("--computer-name", default="")
	facts.add_argument("--host-timezone", default="",
	                   help="IANA or Windows name")
	facts.add_argument("--server-ips", default="", help="comma separated")
	facts.add_argument("--busy-ports", default="",
	                   help="port[=who],… listening on this computer")
	facts.add_argument("--account", default="")
	given = p.add_argument_group("answers (asked when missing, unless --defaults)")
	given.add_argument("--hostname")
	given.add_argument("--https-port")
	given.add_argument("--monitoring", help="y/n")
	given.add_argument("--org-certificate", help="y/n")
	given.add_argument("--timezone")
	p.add_argument("--yes", action="store_true", help="accept the licence terms")
	p.add_argument("--defaults", action="store_true",
	               help="never ask: defaults for what isn't given")
	p.add_argument("--dev", action="store_true",
	               help="a developer's .env + config/runtime.env (repo)")
	return p.parse_args(argv)


def facts_from(args) -> A.Facts:
	busy = {}
	for item in filter(None, (x.strip() for x in args.busy_ports.split(","))):
		port, _, who = item.partition("=")
		if port.isdigit():
			busy[int(port)] = who.strip()
	return A.Facts(os=args.os, computer_name=args.computer_name,
	               timezone=args.host_timezone,
	               server_ips=[ip.strip() for ip in args.server_ips.split(",")
	                           if ip.strip()],
	               busy_ports=busy, account=args.account)


def main(argv=None, read=input, write=print) -> int:
	args = parse_args(sys.argv[1:] if argv is None else argv)
	if args.dev:
		try:
			for line in files.install_dev():
				write(line)
		except files.Refused as e:
			write(str(e))
			return REFUSED
		return OK
	facts = facts_from(args)
	given = {k: getattr(args, k) for k in ANSWER_FLAGS
	         if getattr(args, k) is not None}
	interactive = args.command == "init" and not args.defaults
	if args.command == "init" and files.env_path().exists():
		write(f"NetRollout is already installed here ({files.env_path()}) - "
		      f"see `netrollout status`, or `netrollout update`.")
		return REFUSED
	if args.command == "init" and not A.accept_licence(
			args.os, args.yes, interactive, read, write):
		write("The licence terms weren't accepted - nothing was installed.")
		return INVALID
	try:
		answers = A.collect(facts, given, interactive, read, write)
	except A.Invalid as e:
		for line in str(e).splitlines():
			write(line)
		return INVALID
	except A.NoAnswer as e:
		write(f"No answer for '{e}' (input ended) - run it in a terminal, or "
		      f"give the answers as flags with --defaults.")
		return INVALID
	if args.command == "check":
		write("ok")
		return OK
	try:
		for line in files.install(answers, facts):
			write(line)
	except files.Refused as e:
		write(str(e))
		return REFUSED
	except OSError as e:
		write(f"Couldn't write {e.filename or 'the install folder'}: "
		      f"{e.strerror or e}. Nothing is installed yet - fix it and run "
		      f"the install again.")
		return INVALID
	return OK


if __name__ == "__main__":
	sys.exit(main())
