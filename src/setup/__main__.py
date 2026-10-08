"""python -m src.setup init | check | prepare-start | status | restore-key |
check-update | upgrade | release | port-ready | port-next | port-open |
port-trying | port-close - see src/setup/__init__.py."""
import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from packaging.version import Version

from src import runtime
from src.setup import install, manage, port, update
from src.setup.env import env_path

OK, INVALID, REFUSED = 0, 1, 2
ANSWER_FLAGS = ("hostname", "https_port", "monitoring", "org_certificate",
                "timezone")


def parse_args(argv: list[str]) -> argparse.Namespace:
	""":returns: the command and its flags
	:raises SystemExit: bad arguments (argparse says why)"""
	p = argparse.ArgumentParser(prog="python -m src.setup")
	p.add_argument("command", choices=tuple(COMMANDS))
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
	p.add_argument("--licence-accepted", action="store_true",
	               help="the script showed the licence notice and it was accepted")
	seen = p.add_argument_group("status (from the host script)")
	seen.add_argument("--containers", default="", help="service=state[/health],…")
	seen.add_argument("--reachable", choices=("yes", "no"),
	                  help="the address answered from this computer")
	seen.add_argument("--health-url", default=manage.HEALTH_URL)
	p.add_argument("--defaults", action="store_true",
	               help="never ask: defaults for what isn't given")
	check_update = p.add_argument_group("check-update (run with the installed version's image)")
	check_update.add_argument("--installed", help="the installed version")
	check_update.add_argument("--new", help="the version about to be installed")
	rel = p.add_argument_group("release (Linux's update; run with the installed version's image)")
	rel.add_argument("--check", action="store_true", help="only say whether a newer one exists")
	rel.add_argument("--release-version", help="this version, not the latest")
	rel.add_argument("--feed", help="a mirror's release JSON (URL or file) instead of GitHub")
	rel.add_argument("--from-zip", help="a release zip given by hand (offline)")
	rel.add_argument("--out", default="/install/.update", help="where it's unpacked")
	ph = p.add_argument_group("port-* (the port helper; src/setup/port.py)")
	ph.add_argument("--port", type=int, help="port-open / port-trying: the new port")
	ph.add_argument("--id", default="", help="the request id")
	ph.add_argument("--outcome", choices=("keep", "rollback", "failed"),
	                help="port-close: how the trial ends")
	ph.add_argument("--message", default="", help="port-close: why (rollback, failed)")
	ph.add_argument("--timed-out", action="store_true",
	                help="port-close: the trial's time ran out (the script's stopwatch)")
	p.add_argument("--dev", action="store_true",
	               help="a developer's .env + config/runtime.env (repo)")
	return p.parse_args(argv)


def facts_from(args: argparse.Namespace) -> install.Facts:
	""":returns: what the host script told about the computer"""
	busy: dict[int, str] = {}
	listed: str = args.busy_ports
	for item in filter(None, (x.strip() for x in listed.split(","))):
		port, _, who = item.partition("=")
		if port.isascii() and port.isdigit():
			busy[int(port)] = who.strip()
	return install.Facts(os=args.os, computer_name=args.computer_name,
	               timezone=args.host_timezone,
	               server_ips=[ip.strip() for ip in args.server_ips.split(",")
	                           if ip.strip()],
	               busy_ports=busy, account=args.account)


def main(argv: list[str] | None = None, read: install.Read = input,
         write: install.Write = print) -> int:
	"""Run one setup command.

	:param argv: the arguments; sys.argv's when None
	:param read: how a question is asked (tests answer)
	:param write: where every line goes (tests read it)
	:returns: the exit code - OK, INVALID (bad input, not installed), or
	 REFUSED (already installed, an older version)"""
	args = parse_args(sys.argv[1:] if argv is None else argv)
	if args.dev:
		try:
			for line in install.install_dev():
				write(line)
		except install.Refused as e:
			write(str(e))
			return REFUSED
		return OK
	return COMMANDS[args.command](args, facts_from(args), read, write)


def _installed(write: install.Write, then: str = "") -> bool:
	""":param then: what the "isn't installed" line adds
	:returns: whether .env is there; when not, says so"""
	if env_path().exists():
		return True
	write(f"NetRollout isn't installed here ({env_path()} is missing){then}.")
	return False


def _install(args: argparse.Namespace, facts: install.Facts, read: install.Read,
             write: install.Write) -> int:
	"""init: the answers (asked unless --defaults) and the install; check: the
	answers only (prints ok).

	:returns: the exit code"""
	given = {k: getattr(args, k) for k in ANSWER_FLAGS
	         if getattr(args, k) is not None}
	interactive = args.command == "init" and not args.defaults
	if args.command == "init" and env_path().exists():
		write(f"NetRollout is already installed here ({env_path()}) - "
		      f"see `netrollout status`, or `netrollout update`.")
		return REFUSED
	if args.command == "init" and not args.licence_accepted:
		write("The licence notice wasn't accepted - run the install script, "
		      "which shows it (unattended: its --yes).")
		return INVALID
	try:
		answers = install.collect(facts, given, interactive, read, write)
	except install.Invalid as e:
		for line in str(e).splitlines():
			write(line)
		return INVALID
	except install.NoAnswer as e:
		write(f"No answer for '{e}' (input ended) - run it in a terminal, or "
		      f"give the answers as flags with --defaults.")
		return INVALID
	if args.command == "check":
		write("ok")
		return OK
	try:
		for line in install.install(answers, facts):
			write(line)
	except install.Refused as e:
		write(str(e))
		return REFUSED
	except OSError as e:
		write(f"Couldn't write {e.filename or 'the install folder'}: "
		      f"{e.strerror or e}. Nothing is installed yet - fix it and run "
		      f"the install again.")
		return INVALID
	return OK


def _check_update(args: argparse.Namespace, _facts: install.Facts, _read: install.Read,
                  write: install.Write) -> int:
	"""Prints "update" or "same" (a repair); refused: why, exit 2.

	:returns: the exit code"""
	if not (args.installed and args.new):
		write("check-update needs --installed and --new")
		return INVALID
	try:
		write(update.update_kind(args.installed, args.new))
	except ValueError as e:
		write(str(e))
		return REFUSED
	return OK


def _prepare_start(_args: argparse.Namespace, facts: install.Facts, _read: install.Read,
                   write: install.Write) -> int:
	""":returns: the exit code"""
	if not _installed(write, " - run the install first"):
		return INVALID
	for line in manage.prepare_start(facts.busy_ports, facts.server_ips):
		write(line)
	return OK


def _upgrade(_args: argparse.Namespace, _facts: install.Facts, _read: install.Read,
             write: install.Write) -> int:
	""":returns: the exit code"""
	if not _installed(write, " - run the install first"):
		return INVALID
	try:
		for line in update.upgrade():
			write(line)
	except ValueError as e:
		write(str(e))
		return INVALID
	return OK


def _restore_key(_args: argparse.Namespace, _facts: install.Facts, _read: install.Read,
                 write: install.Write) -> int:
	""":returns: the exit code"""
	if not _installed(write, " - run the install first"):
		return INVALID
	try:
		write(manage.restore_key())
	except ValueError as e:
		write(str(e))
		return INVALID
	return OK


def _status(args: argparse.Namespace, facts: install.Facts, _read: install.Read,
            write: install.Write) -> int:
	""":returns: OK when all is well, else INVALID"""
	if not _installed(write, " - run the install first"):
		return INVALID
	seen = manage.Observed(containers=manage.parse_containers(args.containers),
	                       reachable=None if args.reachable is None
	                       else args.reachable == "yes",
	                       busy=facts.busy_ports)
	lines, well = manage.status(seen, manage.fetch_health(args.health_url))
	for line in lines:
		write(line)
	return OK if well else INVALID


def _port(args: argparse.Namespace, facts: install.Facts, _read: install.Read,
          write: install.Write) -> int:
	"""port-next prints "<action> <port|-> <id|->" and, when there is one, a
	second line saying why; the others do their step and print nothing.

	:returns: the exit code"""
	if not _installed(write):
		return INVALID
	if args.command == "port-ready":
		port.ready()
	elif args.command == "port-next":
		step = port.next_step(facts.busy_ports)
		write(f"{step.action} {step.port or '-'} {step.id or '-'}")
		if step.message:
			write(step.message)
	elif args.command == "port-open":
		port.open_trial(args.port)
	elif args.command == "port-trying":
		port.trying(args.port, args.id)
	else:
		if not args.outcome:
			write("port-close needs --outcome keep|rollback|failed")
			return INVALID
		port.close(args.outcome, args.id, args.message, timed_out_=args.timed_out)
	return OK


def _release(args: argparse.Namespace, _facts: install.Facts, _read: install.Read,
             write: install.Write) -> int:
	"""--check: whether a newer one exists; else downloaded (or given), checked
	and unpacked under --out; prints version=… and folder=… for the script.

	:returns: the exit code"""
	out = Path(args.out)
	try:
		if args.from_zip:
			folder, version = update.unpack(Path(args.from_zip), out)
		else:
			found = update.find(args.release_version, args.feed)
			if args.check:
				if Version(found.version) > Version(runtime.VERSION):
					write(f"NetRollout {found.version} is available (you have "
					      f"{runtime.VERSION}): sudo bin/netrollout.sh update")
				else:
					write(f"You have the latest version ({runtime.VERSION}).")
				return OK
			folder, version = update.unpack(update.download(found, out), out)
	except update.ReleaseError as e:
		write(str(e))
		return INVALID
	write(f"version={version}")
	write(f"folder={folder}")
	return OK


# Each command's handler: (args, facts, read, write) -> the exit code
Handler = Callable[[argparse.Namespace, install.Facts, install.Read, install.Write], int]
COMMANDS: dict[str, Handler] = {
	"init": _install, "check": _install, "prepare-start": _prepare_start, "status": _status,
	"restore-key": _restore_key, "check-update": _check_update, "upgrade": _upgrade,
	"release": _release, "port-ready": _port, "port-next": _port, "port-open": _port,
	"port-trying": _port, "port-close": _port,
}


if __name__ == "__main__":
	sys.exit(main())
