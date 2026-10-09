"""python -m src.backup create | check | restore - the scripts' backup and restore
(windows/manage.ps1, linux/netrollout.sh)."""
import argparse
import sys
from enum import IntEnum
from pathlib import Path

from sqlalchemy.engine import Engine

from src import runtime
from src.backup.archive import (KINDS, BackupError, BackupKind, FilesIncomplete, check, create, restore,
                                shown)
from src.db.connections import PostgresConfig, PostgresConnection, load_config


class BackupExit(IntEnum):
	"""`python -m src.backup`'s exit codes - the scripts branch on them."""
	OK = 0
	FAILED = 1                # refused or failed - nothing changed
	FILES_INCOMPLETE = 3      # restore: the database and key done, not every file


# ── Command line ─────────────────────────────────────────────────────────────

def _app_engine() -> Engine:
	"""The database the app uses: config/runtime.env (a move to an
	organisation's database) over the environment, as the app resolves it."""
	load_config(runtime.runtime_env())
	return PostgresConnection._build_engine(PostgresConfig.unload_env())


def _resolve(name: str) -> Path:
	""":returns: the path given, else a file of that name in the backups folder"""
	path = Path(name)
	if not path.is_absolute() and not path.exists():
		path = runtime.backups_dir() / name
	return path


def main(argv: list[str] | None = None) -> BackupExit:
	"""`create`, `check` or `restore` a backup (the scripts call it).

	:param argv: the arguments; sys.argv's when None
	:returns: the exit code: 0 done, 1 refused or failed - nothing changed
	 (the reason on stderr), 3 (restore) the database restored and its key
	 handed over (--key-out), but not every file (which, on stderr)"""
	parser = argparse.ArgumentParser(prog="python -m src.backup")
	sub = parser.add_subparsers(dest="command", required=True)
	make = sub.add_parser("create")
	make.add_argument("--kind", choices=KINDS, default=BackupKind.MANUAL.value)
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
				               grafana_target=args.grafana_dir, key_out=args.key_out)
			finally:
				engine.dispose()
			print(f"Restored: {shown(_resolve(args.backup))} "
			      f"(NetRollout {done.manifest.version}, {done.manifest.created})")
	except FilesIncomplete as e:
		print(str(e), file=sys.stderr)
		return BackupExit.FILES_INCOMPLETE
	except BackupError as e:
		print(str(e), file=sys.stderr)
		return BackupExit.FAILED
	return BackupExit.OK

if __name__ == "__main__":
	sys.exit(main())
