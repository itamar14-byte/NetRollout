"""Backups: one zip that brings NetRollout back — on this computer or on a
fresh install, whatever database it uses (the bundled one or the
organisation's own).

  netrollout-<version>-<YYYYMMDD-HHMMSS>-<kind>.zip
    manifest.json   format, version, created, kind, migration level, tables
    db/<table>.csv  each NetRollout table, one consistent snapshot
    grafana.db      Grafana's database (the admins' Custom dashboards)
    certs/          the certificate, its key, the markers
    logs/           the rollout log files
    encryption.key  decrypts the saved credentials (the zip is as secret as .env)

The database goes through the app's own connection, never a superuser: the
backup reads NetRollout's tables in one read-only snapshot; the restore, in
one transaction, undoes NetRollout's migrations (its own tables only, nothing
else in that database), migrates to the backup's level, loads the data and
checks the key decrypts it — any failure leaves the database as it was. The
app migrates to its own level at its next start, so an older backup restores
onto a newer NetRollout; a newer one is refused.

What stays this computer's: the passwords and settings in .env, the port (the
HTTPS port setting is set to the one in use), config/ (the app writes
site.env from the restored settings at its start).

  python -m src.backup create [--kind manual]
  python -m src.backup check <backup>
  python -m src.backup restore <backup> [--grafana-dir DIR] [--https-port N]
                                        [--key-out FILE]
"""
import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from packaging.version import InvalidVersion, Version
from sqlalchemy import MetaData, inspect, insert, text, update
from sqlalchemy.engine import Connection, Engine

from src import runtime
from src.db.backend import ENCRYPTED_COLUMNS, FERNET_PREFIX
from src.db.postgres_db import PostgresConfig, PostgresConnection
from src.db.tables import AuditLog, Base, SystemSetting
from src.encryption import read_key

FORMAT = 1
KINDS = ("manual", "scheduled", "before-restore", "before-update", "before-move")
NAME_RE = re.compile(r"^netrollout-(?P<version>[0-9A-Za-z.+]+)-"
                     r"(?P<stamp>\d{8}-\d{6})-(?P<kind>[a-z-]+)\.zip$")
MANIFEST = "manifest.json"
KEY_MEMBER = "encryption.key"
GRAFANA_MEMBER = "grafana.db"
# the certificate folder's files a backup carries (nothing else is there)
CERT_FILES = ("fullchain.pem", "privkey.pem", ".selfsigned", ".old-names.json")
LOG_RE = re.compile(r"^(rollout_.+\.log|unsaved-results-.+\.json)$")
LOCK = ".backup.lock"
# a backup from elsewhere, staged in the backups folder by the scripts
STAGED_PREFIX = ".restoring-"
STALE_LOCK_SECONDS = 2 * 3600
ALEMBIC_INI = Path(__file__).resolve().parent / "db" / "alembic.ini"


class BackupError(Exception):
	"""What went wrong, in words for the person running it."""


def shown(path: Path) -> str:
	"""The name people chose (a staged copy's, without the prefix)."""
	return path.name.removeprefix(STAGED_PREFIX)


@dataclass
class Manifest:
	format: int
	version: str
	created: str            # local time, ISO 8601
	kind: str
	revision: str           # the database's migration level
	tables: dict[str, int]  # table → rows
	grafana: bool
	certs: list[str]
	logs: int

	@classmethod
	def from_json(cls, raw: bytes) -> "Manifest":
		data = json.loads(raw)
		return cls(**{k: data[k] for k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class Places:
	"""Where the files are: the app's folders by default (tests pass others)."""
	backups: Path
	certs: Path
	logs: Path
	grafana: Path | None = None    # Grafana's data folder, if any

	@classmethod
	def app(cls) -> "Places":
		return cls(runtime.backups_dir(), runtime.certs_dir(), runtime.logs_dir(),
		           runtime.grafana_dir())


@dataclass
class Entry:
	"""One file in the backups folder, for the list."""
	path: Path
	size: int
	manifest: Manifest | None = None
	problem: str | None = None

	@property
	def name(self) -> str:
		return self.path.name


@dataclass
class Restored:
	manifest: Manifest
	key: bytes
	files: list[str] = field(default_factory=list)


# ── Create ───────────────────────────────────────────────────────────────────

def create(engine: Engine, kind: str = "manual", places: Places | None = None,
           *, key: bytes | None = None, now: datetime | None = None) -> Path:
	"""Write a backup into places.backups; returns its path.
	:raises BackupError: another backup running, no encryption key, Grafana's
	 database unreadable, a database error"""
	if kind not in KINDS:
		raise ValueError(f"unknown kind {kind!r}")
	places = places or Places.app()
	key = key if key is not None else _current_key()
	now = now or datetime.now()
	name = f"netrollout-{runtime.VERSION}-{now:%Y%m%d-%H%M%S}-{kind}.zip"
	places.backups.mkdir(parents=True, exist_ok=True)
	final = places.backups / name
	partial = places.backups / f".{name}.partial"
	with _lock(places.backups):
		try:
			with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
				revision, tables = _dump_database(engine, zf)
				grafana = _add_grafana(zf, places.grafana)
				certs = _add_files(zf, places.certs, "certs",
				                   lambda n: n in CERT_FILES)
				logs = _add_files(zf, places.logs, "logs", LOG_RE.match)
				zf.writestr(KEY_MEMBER, key)
				manifest = Manifest(FORMAT, runtime.VERSION,
				                    now.isoformat(timespec="seconds"), kind,
				                    revision, tables, grafana, certs, len(logs))
				zf.writestr(MANIFEST, json.dumps(asdict(manifest), indent=2))
			_private(partial)
			os.replace(partial, final)
		except BaseException:
			partial.unlink(missing_ok=True)
			raise
	return final


def _current_key() -> bytes:
	key = read_key()
	if not key:
		raise BackupError("No encryption key: the saved credentials couldn't be "
		                  "restored from this backup, so it isn't made.")
	return key


def _our_tables(conn: Connection) -> list[str]:
	"""NetRollout's tables in this database (the schema may hold others')."""
	existing = set(inspect(conn).get_table_names())
	return [t.name for t in Base.metadata.sorted_tables if t.name in existing]


def _dump_database(engine: Engine, zf: zipfile.ZipFile) -> tuple[str, dict]:
	with engine.connect() as conn:
		conn = conn.execution_options(isolation_level="REPEATABLE READ")
		with conn.begin():
			conn.execute(text("SET TRANSACTION READ ONLY"))
			revision = conn.execute(
				text("SELECT version_num FROM alembic_version")).scalar()
			if not revision:
				raise BackupError("The database has no NetRollout tables yet "
				                  "(start NetRollout once first).")
			cursor = conn.connection.dbapi_connection.cursor()
			tables = {}
			for table in _our_tables(conn):
				tables[table] = conn.execute(
					text(f'SELECT count(*) FROM "{table}"')).scalar()
				with zf.open(f"db/{table}.csv", "w") as raw, \
						io.TextIOWrapper(raw, encoding="utf-8", newline="") as out:
					cursor.copy_expert(f'COPY (SELECT * FROM "{table}") TO STDOUT '
					                   f"WITH (FORMAT csv, HEADER)", out)
			return revision, tables


def _add_grafana(zf: zipfile.ZipFile, folder: Path | None) -> bool:
	source = folder / GRAFANA_MEMBER if folder else None
	if not source or not source.is_file():
		return False
	# SQLite's online backup: a consistent copy while Grafana keeps running
	with tempfile.TemporaryDirectory() as tmp:
		copy = Path(tmp) / GRAFANA_MEMBER
		try:
			src = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
			try:
				dst = sqlite3.connect(copy)
				with dst:
					src.backup(dst)
				dst.close()
			finally:
				src.close()
		except sqlite3.Error as e:
			raise BackupError(f"Grafana's database couldn't be read ({e}) - try "
			                  f"again in a minute.") from None
		zf.write(copy, GRAFANA_MEMBER)
	return True


def _add_files(zf: zipfile.ZipFile, folder: Path, prefix: str, wanted) -> list[str]:
	if not folder.is_dir():
		return []
	names = sorted(p.name for p in folder.iterdir() if p.is_file() and wanted(p.name))
	for name in names:
		zf.write(folder / name, f"{prefix}/{name}")
	return names


def _private(path: Path) -> None:
	# it holds the encryption key: owner only (no effect on Windows, where
	# the backups folder's permissions do it)
	try:
		os.chmod(path, 0o600)
	except OSError:
		pass


class _lock:
	"""One backup or restore at a time per backups folder (a scheduled one
	and a manual one can meet). A lock left by a crash expires."""

	def __init__(self, folder: Path):
		self.path = folder / LOCK

	def __enter__(self):
		self.path.parent.mkdir(parents=True, exist_ok=True)
		try:
			if time.time() - self.path.stat().st_mtime > STALE_LOCK_SECONDS:
				self.path.unlink(missing_ok=True)
		except FileNotFoundError:
			pass
		try:
			os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
		except FileExistsError:
			raise BackupError("Another backup or restore is running - try again "
			                  "when it's done.") from None
		return self

	def __exit__(self, *exc):
		self.path.unlink(missing_ok=True)


# ── Read / check ─────────────────────────────────────────────────────────────

def read_manifest(path: Path) -> Manifest:
	""":raises BackupError: not a NetRollout backup, or damaged"""
	try:
		with zipfile.ZipFile(path) as zf:
			return Manifest.from_json(zf.read(MANIFEST))
	except FileNotFoundError:
		raise BackupError(f"{path} doesn't exist.") from None
	except (zipfile.BadZipFile, KeyError, ValueError, TypeError) as e:
		raise BackupError(f"{shown(path)} isn't a NetRollout backup, or it's "
		                  f"damaged ({e}).") from None


def _alembic_config(conn: Connection | None = None) -> AlembicConfig:
	cfg = AlembicConfig(str(ALEMBIC_INI))
	cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
	if conn is not None:
		cfg.attributes["connection"] = conn
	return cfg


def check(path: Path, version: str = runtime.VERSION) -> Manifest:
	"""Whether this NetRollout can restore the backup.
	:raises BackupError: why not, in words"""
	manifest = read_manifest(path)
	if manifest.format != FORMAT:
		raise BackupError(f"{shown(path)} has backup format {manifest.format}; this "
		                  f"NetRollout reads format {FORMAT}.")
	try:
		newer = Version(manifest.version) > Version(version)
	except InvalidVersion:
		newer = True
	if newer:
		raise BackupError(f"{shown(path)} was made by NetRollout {manifest.version}, "
		                  f"newer than this one ({version}). Update NetRollout "
		                  f"first, then restore.")
	try:
		known = ScriptDirectory.from_config(_alembic_config()).get_revision(
			manifest.revision)
	except Exception:
		known = None
	if known is None:
		raise BackupError(f"{shown(path)} has a database level ({manifest.revision}) "
		                  f"this NetRollout doesn't know.")
	with zipfile.ZipFile(path) as zf:
		members = set(zf.namelist())
	missing = [m for m in [f"db/{t}.csv" for t in manifest.tables] + [KEY_MEMBER]
	           if m not in members]
	if manifest.grafana and GRAFANA_MEMBER not in members:
		missing.append(GRAFANA_MEMBER)
	if missing:
		raise BackupError(f"{shown(path)} is incomplete (missing {', '.join(missing)}).")
	return manifest


def list_backups(folder: Path | None = None) -> list[Entry]:
	"""The backups in the folder, newest first (by the time in the name)."""
	folder = folder or runtime.backups_dir()
	if not folder.is_dir():
		return []
	entries = []
	for path in folder.iterdir():
		if not (path.is_file() and NAME_RE.match(path.name)):
			continue
		entry = Entry(path, path.stat().st_size)
		try:
			entry.manifest = read_manifest(path)
		except BackupError as e:
			entry.problem = str(e)
		entries.append(entry)
	return sorted(entries, key=lambda e: NAME_RE.match(e.name)["stamp"], reverse=True)


def prune(folder: Path, keep: int) -> list[Path]:
	"""Scheduled backups beyond the newest `keep` are deleted; the others
	(manual, before a restore or update) only by a person. Returns what went."""
	scheduled = [e.path for e in list_backups(folder)
	             if NAME_RE.match(e.name)["kind"] == "scheduled"]
	gone = scheduled[max(keep, 0):]
	for path in gone:
		path.unlink(missing_ok=True)
	return gone


# ── Restore ──────────────────────────────────────────────────────────────────

def restore(path: Path, engine: Engine, places: Places | None = None, *,
            https_port: int | None = None, grafana_target: Path | None = None,
            version: str = runtime.VERSION) -> Restored:
	"""Restore the backup into the database `engine` connects to, then its
	files. NetRollout must be stopped (the scripts do it). Returns the
	backup's encryption key: the caller puts it where the app reads it.
	:param https_port: the port this install serves on (the setting follows)
	:param grafana_target: Grafana's data folder (Grafana stopped), if any
	:raises BackupError: refused or failed — the database is unchanged"""
	places = places or Places.app()
	manifest = check(path, version)
	with _lock(places.backups), zipfile.ZipFile(path) as zf:
		key = zf.read(KEY_MEMBER).strip()
		try:
			cipher = Fernet(key)
		except ValueError:
			raise BackupError(f"{shown(path)} holds a damaged encryption key.") from None
		_restore_database(engine, zf, manifest, cipher, https_port,
		                  ("backup.restored", {"version": manifest.version,
		                                       "created": manifest.created,
		                                       "kind": manifest.kind}), shown(path))
		files = []
		files += _restore_files(zf, "certs", places.certs, set(CERT_FILES),
		                        replace_all=True)
		files += _restore_files(zf, "logs", places.logs, None)
		if manifest.grafana and grafana_target is not None:
			files += _restore_grafana(zf, grafana_target)
	return Restored(manifest, key, files)


def restore_database(path: Path, engine: Engine, *, audit: tuple[str, dict],
                     places: Places | None = None, version: str = runtime.VERSION) -> Manifest:
	"""Only the database part of a backup, into the database `engine`
	connects to (a database move: the files stay where they are), with the
	backup's key checked against its credentials. audit: the (action,
	detail) row recorded in the same transaction.
	:raises BackupError: refused or failed - the database is unchanged"""
	places = places or Places.app()
	manifest = check(path, version)
	with _lock(places.backups), zipfile.ZipFile(path) as zf:
		try:
			cipher = Fernet(zf.read(KEY_MEMBER).strip())
		except ValueError:
			raise BackupError(f"{shown(path)} holds a damaged encryption key.") from None
		_restore_database(engine, zf, manifest, cipher, None, audit, shown(path))
	return manifest


def _restore_database(engine, zf, manifest, cipher, https_port, audit, name) -> None:
	with engine.begin() as conn:
		conn.execute(text("SET LOCAL lock_timeout = '15s'"))
		cfg = _alembic_config(conn)
		# NetRollout's tables only, by its own migrations: anything else in
		# this database (an organisation's schema) is never touched
		alembic_command.downgrade(cfg, "base")
		alembic_command.upgrade(cfg, manifest.revision)
		meta = MetaData()
		meta.reflect(conn, only=list(manifest.tables))
		cursor = conn.connection.dbapi_connection.cursor()
		for table in meta.sorted_tables:   # parents before children
			with zf.open(f"db/{table.name}.csv") as raw, \
					io.TextIOWrapper(raw, encoding="utf-8", newline="") as data:
				columns = next(csv.reader([data.readline()]), [])
				if not columns:
					continue
				listed = ", ".join(f'"{c}"' for c in columns)
				cursor.copy_expert(f'COPY "{table.name}" ({listed}) FROM STDIN '
				                   f"WITH (FORMAT csv)", data)
		_reset_sequences(conn, meta)
		_check_key(conn, cipher)
		if https_port is not None:
			conn.execute(update(SystemSetting.__table__)
			             .where(SystemSetting.__table__.c.key == "https_port")
			             .values(value=https_port))
		action, detail = audit
		conn.execute(insert(AuditLog.__table__).values(
			actor_username="netrollout", action=action,
			object_type="backup", object_label=name, success=True,
			detail=detail))


def _reset_sequences(conn: Connection, meta: MetaData) -> None:
	# the next id after the loaded rows (a COPY doesn't move a sequence)
	for table in meta.sorted_tables:
		for column in table.columns:
			sequence = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
			                        {"t": f'"{table.name}"', "c": column.name}).scalar()
			if sequence:
				conn.execute(text(
					f'SELECT setval(:s, COALESCE((SELECT max("{column.name}") FROM '
					f'"{table.name}"), 1), (SELECT max("{column.name}") FROM '
					f'"{table.name}") IS NOT NULL)'), {"s": sequence})


def _check_key(conn: Connection, cipher: Fernet) -> None:
	"""The backup's key must decrypt the backup's credentials: a mismatch
	would leave every saved password unusable."""
	present = {t: {c["name"] for c in inspect(conn).get_columns(t)}
	           for t in {col.table.name for col in ENCRYPTED_COLUMNS}
	           if inspect(conn).has_table(t)}
	for column in ENCRYPTED_COLUMNS:
		table, name = column.table.name, column.name
		if name not in present.get(table, ()):
			continue
		sample = conn.execute(text(
			f'SELECT "{name}" FROM "{table}" WHERE "{name}" LIKE :p LIMIT 1'),
			{"p": f"{FERNET_PREFIX}%"}).scalar()
		if sample:
			try:
				cipher.decrypt(sample.encode())
			except InvalidToken:
				raise BackupError("The backup's encryption key doesn't decrypt its "
				                  "saved credentials - the backup is damaged. "
				                  "Nothing was changed.") from None
			return


def _restore_files(zf, prefix: str, folder: Path, allowed: set | None,
                   replace_all: bool = False) -> list[str]:
	"""The backup's files of one folder. replace_all: the folder's files of
	that kind that the backup doesn't have go too (certs: a marker left from
	another certificate would mislead the app)."""
	members = {Path(m).name: m for m in zf.namelist()
	           if m.startswith(prefix + "/") and not m.endswith("/")}
	names = {n for n in members if (allowed is None and LOG_RE.match(n))
	         or (allowed is not None and n in allowed)}
	if not names:
		return []     # (a backup without certificates keeps this one's)
	folder.mkdir(parents=True, exist_ok=True)
	if replace_all and allowed:
		for stale in allowed - names:
			(folder / stale).unlink(missing_ok=True)
	written = []
	for name in sorted(names):
		_write_like_folder(folder / name, zf.read(members[name]), folder)
		written.append(f"{prefix}/{name}")
	return written


def _restore_grafana(zf, folder: Path) -> list[str]:
	folder.mkdir(parents=True, exist_ok=True)
	for leftover in ("-journal", "-wal", "-shm"):
		(folder / (GRAFANA_MEMBER + leftover)).unlink(missing_ok=True)
	_write_like_folder(folder / GRAFANA_MEMBER, zf.read(GRAFANA_MEMBER), folder)
	return [GRAFANA_MEMBER]


def _write_like_folder(target: Path, data: bytes, folder: Path) -> None:
	"""Atomically; run as root (the scripts' restore, so it can write into
	Grafana's volume) the file gets the folder's owner — the service that
	uses it."""
	tmp = target.with_name(f".{target.name}.restoring")
	tmp.write_bytes(data)
	if target.name == "privkey.pem":
		_private(tmp)
	elif target.name == GRAFANA_MEMBER:
		# as Grafana keeps it: its owner, and group 0 reads (the app's backups)
		os.chmod(tmp, 0o640)
	if hasattr(os, "geteuid") and os.geteuid() == 0:
		owner = folder.stat()
		os.chown(tmp, owner.st_uid, owner.st_gid)
	os.replace(tmp, target)


# ── Command line ─────────────────────────────────────────────────────────────

def _app_engine() -> Engine:
	"""The database the app uses: config/runtime.env (a move to an
	organisation's database) over the environment, as the app resolves it."""
	load_dotenv(runtime.runtime_env(), override=True)
	return PostgresConnection._build_engine(PostgresConfig.unload_env())


def _resolve(name: str) -> Path:
	path = Path(name)
	if not path.is_absolute() and not path.exists():
		path = runtime.backups_dir() / name
	return path


def main(argv=None) -> int:
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
				_private(args.key_out)
			print(f"Restored: {shown(_resolve(args.backup))} "
			      f"(NetRollout {done.manifest.version}, {done.manifest.created})")
	except BackupError as e:
		print(str(e), file=sys.stderr)
		return 1
	return 0


if __name__ == "__main__":
	sys.exit(main())
