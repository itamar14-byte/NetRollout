"""python -m src.backup create | check | restore - the scripts' backup and restore
(windows/manage.ps1, linux/netrollout.sh)."""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import Engine

from src import runtime
from src.backup.archive import KINDS, BackupError, owner_only, check, create, restore, shown
from src.db.connections import PostgresConfig, PostgresConnection


# ── Command line ─────────────────────────────────────────────────────────────

def _app_engine() -> Engine:
	"""The database the app uses: config/runtime.env (a move to an
	organisation's database) over the environment, as the app resolves it."""
	load_dotenv(runtime.runtime_env(), override=True)
	return PostgresConnection._build_engine(PostgresConfig.unload_env())


def _resolve(name: str) -> Path:
	""":returns: the path given, else a file of that name in the backups folder"""
	path = Path(name)
	if not path.is_absolute() and not path.exists():
		path = runtime.backups_dir() / name
	return path


def main(argv: list[str] | None = None) -> int:
	"""`create`, `check` or `restore` a backup (the scripts call it).

	:param argv: the arguments; sys.argv's when None
	:returns: the exit code: 0 done, 1 refused or failed (the reason on
	 stderr)"""
	parser = argparse.ArgumentParser(prog="python -m src.backup")
	sub = parser.add_subparsers(dest="command", required=True)
	make = sub.add_parser("create")
	make.add_argument("--kind", choices=KINDS, default="manual")
	look = sub.add_parser("check")
	look.add_argument("backup")
	back = sub.add_parser("restore")
	back.add_argument("backup")
	back.add_argument("--grafana-dir", type=Path)
	back.add_argument("--https-port", type=int)
	back.add_argument("--key-out", type=Path,
	                  help="write the backup's encryption key here (owner only)")
	args = parser.parse_args(argv)
	try:
		if args.command == "create":
			engine = _app_engine()
			try:
				path = create(engine, args.kind)
			finally:
				engine.dispose()
			print(f"Backed up: {path.name}")
		elif args.command == "check":
			m = check(_resolve(args.backup))
			print(f"NetRollout {m.version}, {m.created} ({m.kind}): "
			      f"{sum(m.tables.values())} database rows, "
			      f"{'Grafana, ' if m.grafana else ''}{m.logs} rollout logs")
		else:
			engine = _app_engine()
			try:
				done = restore(_resolve(args.backup), engine,
				               https_port=args.https_port,
				               grafana_target=args.grafana_dir)
			finally:
				engine.dispose()
			if args.key_out:
				args.key_out.write_bytes(done.key + b"\n")
				owner_only(args.key_out)
			print(f"Restored: {shown(_resolve(args.backup))} "
			      f"(NetRollout {done.manifest.version}, {done.manifest.created})")
	except BackupError as e:
		print(str(e), file=sys.stderr)
		return 1
	return 0

if __name__ == "__main__":
	sys.exit(main())
