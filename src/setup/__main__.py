"""python -m src.setup init | check | prepare-start | status | restore-key |
check-update | upgrade | release | port-ready | port-next | port-open |
port-trying | port-close - see src/setup/__init__.py."""
import argparse
import sys
from pathlib import Path

from packaging.version import Version

from src.setup import answers as A
from src import runtime
from src.setup import files, manage, port, release

OK, INVALID, REFUSED = 0, 1, 2
ANSWER_FLAGS = ("hostname", "https_port", "monitoring", "org_certificate",
                "timezone")


def parse_args(argv):
	p = argparse.ArgumentParser(prog="python -m src.setup")
	p.add_argument("command", choices=("init", "check", "prepare-start", "status",
	                                   "restore-key", "check-update", "upgrade", "release",
	                                   "port-ready", "port-next", "port-open", "port-trying",
	                                   "port-close"))
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
	update = p.add_argument_group("check-update (run with the installed version's image)")
	update.add_argument("--installed", help="the installed version")
	update.add_argument("--new", help="the version about to be installed")
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
	if args.command == "check-update":
		# prints "update" or "same" (a repair); refused: why, exit 2
		if not (args.installed and args.new):
			write("check-update needs --installed and --new")
			return INVALID
		try:
			write(manage.update_kind(args.installed, args.new))
		except ValueError as e:
			write(str(e))
			return REFUSED
		return OK
	if args.command == "release":
		return _release(args, write)
	if args.command.startswith("port-"):
		if not files.env_path().exists():
			write(f"NetRollout isn't installed here ({files.env_path()} is missing).")
			return INVALID
		return _port(args, facts, write)
	if args.command in ("prepare-start", "status", "restore-key", "upgrade"):
		if not files.env_path().exists():
			write(f"NetRollout isn't installed here ({files.env_path()} is missing) "
			      f"- run the install first.")
			return INVALID
		if args.command == "prepare-start":
			for line in manage.prepare_start(facts.busy_ports, facts.server_ips):
				write(line)
			return OK
		if args.command == "upgrade":
			try:
				for line in manage.upgrade():
					write(line)
			except ValueError as e:
				write(str(e))
				return INVALID
			return OK
		if args.command == "restore-key":
			try:
				write(manage.restore_key())
			except ValueError as e:
				write(str(e))
				return INVALID
			return OK
		seen = manage.Observed(containers=manage.parse_containers(args.containers),
		                       reachable=None if args.reachable is None
		                       else args.reachable == "yes",
		                       busy=facts.busy_ports)
		lines, well = manage.status(seen, manage.fetch_health(args.health_url))
		for line in lines:
			write(line)
		return OK if well else INVALID
	given = {k: getattr(args, k) for k in ANSWER_FLAGS
	         if getattr(args, k) is not None}
	interactive = args.command == "init" and not args.defaults
	if args.command == "init" and files.env_path().exists():
		write(f"NetRollout is already installed here ({files.env_path()}) - "
		      f"see `netrollout status`, or `netrollout update`.")
		return REFUSED
	if args.command == "init" and not args.licence_accepted:
		write("The licence notice wasn't accepted - run the install script, "
		      "which shows it (unattended: its --yes).")
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


def _port(args, facts, write) -> int:
	"""port-next prints "<action> <port|-> <id|->" and, when there is one, a
	second line saying why; the others do their step and print nothing."""
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


def _release(args, write) -> int:
	"""--check: whether a newer one exists; else downloaded (or given), checked
	and unpacked under --out; prints version=… and folder=… for the script."""
	out = Path(args.out)
	try:
		if args.from_zip:
			folder, version = release.unpack(Path(args.from_zip), out)
		else:
			found = release.find(args.release_version, args.feed)
			if args.check:
				if Version(found.version) > Version(runtime.VERSION):
					write(f"NetRollout {found.version} is available (you have "
					      f"{runtime.VERSION}): sudo bin/netrollout.sh update")
				else:
					write(f"You have the latest version ({runtime.VERSION}).")
				return OK
			folder, version = release.unpack(release.download(found, out), out)
	except release.ReleaseError as e:
		write(str(e))
		return INVALID
	write(f"version={version}")
	write(f"folder={folder}")
	return OK


if __name__ == "__main__":
	sys.exit(main())
