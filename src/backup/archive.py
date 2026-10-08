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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from cryptography.fernet import Fernet, InvalidToken
from packaging.version import InvalidVersion, Version
from sqlalchemy import MetaData, Table, inspect, insert, text, update
from sqlalchemy.engine import Connection, Engine

from src import runtime
from src.db.connections import ENCRYPTED_COLUMNS, FERNET_PREFIX
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
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "db" / "alembic.ini"


class BackupError(Exception):
	"""What went wrong, in words for the person running it."""


def shown(path: Path) -> str:
	"""The name people chose (a staged copy's, without the prefix)."""
	return path.name.removeprefix(STAGED_PREFIX)


@dataclass
class Manifest:
	"""What a backup holds (its manifest.json)."""
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
		""":raises KeyError: a field is missing
		:raises ValueError: not JSON"""
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
		""":returns: the app's own folders"""
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
		""":returns: the file's name"""
		return self.path.name

	@property
	def kind(self) -> str:
		""":returns: what made it - one of KINDS (from the name)"""
		return self._part("kind")

	@property
	def stamp(self) -> str:
		""":returns: when it was made, YYYYMMDD-HHMMSS (from the name)"""
		return self._part("stamp")

	def _part(self, group: str) -> str:
		match = NAME_RE.match(self.name)
		assert match is not None   # list_backups lists only matching names
		return match[group]


@dataclass
class Restored:
	"""What a restore did: the backup's manifest and key, the files written."""
	manifest: Manifest
	key: bytes
	files: list[str] = field(default_factory=list)


# ── Create ───────────────────────────────────────────────────────────────────

def create(engine: Engine, kind: str = "manual", places: Places | None = None,
           *, key: bytes | None = None, now: datetime | None = None) -> Path:
	"""Write a backup into places.backups: the database (one consistent
	snapshot), Grafana's database, the certificates, the rollout logs and the
	encryption key - written under a temporary name, then renamed.

	:param kind: what makes it - one of KINDS (it's in the name)
	:param places: the folders; the app's when None
	:param key: the encryption key to store; the app's when None
	:param now: the time in its name (tests); now when None
	:returns: its path
	:raises ValueError: an unknown kind
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
			owner_only(partial)
			os.replace(partial, final)
		except BaseException:
			partial.unlink(missing_ok=True)
			raise
	return final


def _current_key() -> bytes:
	""":raises BackupError: there's no key (a backup without it is useless)"""
	key = read_key()
	if not key:
		raise BackupError("No encryption key: the saved credentials couldn't be "
		                  "restored from this backup, so it isn't made.")
	return key


def _our_tables(conn: Connection) -> list[str]:
	""":returns: NetRollout's tables present in the database, parents first"""
	"""NetRollout's tables in this database (the schema may hold others')."""
	existing = set(inspect(conn).get_table_names())
	return [t.name for t in Base.metadata.sorted_tables if t.name in existing]


def _dump_database(engine: Engine, zf: zipfile.ZipFile) -> tuple[str, dict[str, int]]:
	"""Every NetRollout table into the zip as db/<table>.csv, from one
	read-only snapshot (REPEATABLE READ): the tables agree with each other.

	:returns: (the migration level, {table: rows})
	:raises BackupError: the database has no NetRollout tables yet"""
	with engine.connect() as conn:
		conn = conn.execution_options(isolation_level="REPEATABLE READ")
		with conn.begin():
			conn.execute(text("SET TRANSACTION READ ONLY"))
			revision = conn.execute(
				text("SELECT version_num FROM alembic_version")).scalar()
			if not revision:
				raise BackupError("The database has no NetRollout tables yet "
				                  "(start NetRollout once first).")
			cursor = _driver_cursor(conn)
			tables: dict[str, int] = {}
			for table in _our_tables(conn):
				tables[table] = conn.execute(
					text(f'SELECT count(*) FROM "{table}"')).scalar_one()
				with zf.open(f"db/{table}.csv", "w") as raw, \
						io.TextIOWrapper(raw, encoding="utf-8", newline="") as out:
					cursor.copy_expert(f'COPY (SELECT * FROM "{table}") TO STDOUT '
					                   f"WITH (FORMAT csv, HEADER)", out)
			return revision, tables


def _driver_cursor(conn: Connection) -> Any:
	""":returns: the driver's own cursor, for COPY (psycopg2's copy_expert)"""
	dbapi = conn.connection.dbapi_connection
	assert dbapi is not None   # a connection in use always has one
	return dbapi.cursor()


def _add_grafana(zf: zipfile.ZipFile, folder: Path | None) -> bool:
	"""Grafana's database into the zip, copied by SQLite's online backup.

	:param folder: Grafana's data folder; None: there's no Grafana
	:returns: whether there was one to add
	:raises BackupError: it couldn't be read"""
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


def _add_files(zf: zipfile.ZipFile, folder: Path, prefix: str,
               wanted: Callable[[str], object]) -> list[str]:
	"""The folder's files that `wanted` accepts (by name) into the zip under
	`prefix`/.

	:returns: their names; [] when the folder doesn't exist"""
	if not folder.is_dir():
		return []
	names = sorted(p.name for p in folder.iterdir() if p.is_file() and wanted(p.name))
	for name in names:
		zf.write(folder / name, f"{prefix}/{name}")
	return names


def owner_only(path: Path) -> None:
	"""Make a file owner only: it holds the encryption key (a backup, and the
	key file `python -m src.backup restore --key-out` writes). No effect on
	Windows, where the backups folder's permissions do it."""
	try:
		os.chmod(path, 0o600)
	except OSError:
		pass


class _lock:
	"""One backup or restore at a time per backups folder (a scheduled one
	and a manual one can meet). A lock left by a crash expires."""

	def __init__(self, folder: Path) -> None:
		self.path = folder / LOCK

	def __enter__(self) -> "_lock":
		""":raises BackupError: another backup or restore holds it"""
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

	def __exit__(self, *exc: object) -> None:
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
	""":param conn: the connection migrations run on (in its transaction);
	 None: Alembic's own (from alembic.ini)"""
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
	entries: list[Entry] = []
	for path in folder.iterdir():
		if not (path.is_file() and NAME_RE.match(path.name)):
			continue
		entry = Entry(path, path.stat().st_size)
		try:
			entry.manifest = read_manifest(path)
		except BackupError as e:
			entry.problem = str(e)
		entries.append(entry)
	return sorted(entries, key=lambda e: e.stamp, reverse=True)


def prune(folder: Path, keep: int) -> list[Path]:
	"""Scheduled backups beyond the newest `keep` are deleted; the others
	(manual, before a restore or update) only by a person.

	:returns: the deleted files"""
	scheduled = [e.path for e in list_backups(folder) if e.kind == "scheduled"]
	gone = scheduled[max(keep, 0):]
	for path in gone:
		path.unlink(missing_ok=True)
	return gone


# ── Restore ──────────────────────────────────────────────────────────────────

def restore(path: Path, engine: Engine, places: Places | None = None, *,
            https_port: int | None = None, grafana_target: Path | None = None,
            version: str = runtime.VERSION) -> Restored:
	"""Restore the backup into the database `engine` connects to, then its
	files. NetRollout must be stopped (the scripts do it).

	:param places: the folders; the app's when None
	:param https_port: the port this install serves on (the setting follows)
	:param grafana_target: Grafana's data folder (Grafana stopped), if any
	:param version: this NetRollout's (a newer backup is refused)
	:returns: what was restored, with the backup's encryption key - the
	 caller puts it where the app reads it
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
		files: list[str] = []
		files += _restore_files(zf, "certs", places.certs, set(CERT_FILES),
		                        replace_all=True)
		files += _restore_files(zf, "logs", places.logs, None)
		if manifest.grafana and grafana_target is not None:
			files += _restore_grafana(zf, grafana_target)
	return Restored(manifest, key, files)


def restore_database(path: Path, engine: Engine, *, audit: tuple[str, dict[str, Any]],
                     places: Places | None = None, version: str = runtime.VERSION) -> Manifest:
	"""Only the database part of a backup, into the database `engine`
	connects to (a database move: the files stay where they are), with the
	backup's key checked against its credentials.

	:param audit: the (action, detail) row recorded in the same transaction
	:returns: the backup's manifest
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


def _restore_database(engine: Engine, zf: zipfile.ZipFile, manifest: Manifest,
                      cipher: Fernet, https_port: int | None,
                      audit: tuple[str, dict[str, Any]], name: str) -> None:
	"""The backup's tables into the database, in one transaction: NetRollout's
	tables dropped and recreated at the backup's migration level, the rows
	loaded, the sequences moved past them, the key checked.

	:param cipher: the backup's key - it must decrypt a stored credential
	:param https_port: the setting's value afterwards (this install's port);
	 None: the backup's
	:param audit: the (action, detail) row recorded with it
	:param name: the backup's name, for the audit row
	:raises BackupError: the key doesn't match - nothing changed"""
	with engine.begin() as conn:
		conn.execute(text("SET LOCAL lock_timeout = '15s'"))
		cfg = _alembic_config(conn)
		# NetRollout's tables only, by its own migrations: anything else in
		# this database (an organisation's schema) is never touched
		alembic_command.downgrade(cfg, "base")
		alembic_command.upgrade(cfg, manifest.revision)
		meta = MetaData()
		meta.reflect(conn, only=list(manifest.tables))
		cursor = _driver_cursor(conn)
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
			settings = cast(Table, SystemSetting.__table__)
			conn.execute(update(settings).where(settings.c.key == "https_port")
			             .values(value=https_port))
		action, detail = audit
		conn.execute(insert(cast(Table, AuditLog.__table__)).values(
			actor_username="netrollout", action=action,
			object_type="backup", object_label=name, success=True,
			detail=detail))


def _reset_sequences(conn: Connection, meta: MetaData) -> None:
	"""Each serial column's sequence to the next id after the loaded rows (a
	COPY doesn't move a sequence)."""
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
	would leave every saved password unusable. One sample is enough (all
	were encrypted with one key); a backup without credentials passes.

	:raises BackupError: the key doesn't decrypt the sample"""
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


def _restore_files(zf: zipfile.ZipFile, prefix: str, folder: Path,
                   allowed: set[str] | None, replace_all: bool = False) -> list[str]:
	"""The backup's files of one folder.

	:param prefix: their folder in the zip
	:param allowed: the names that may be restored; None: rollout logs
	:param replace_all: the folder's files of that kind that the backup
	 doesn't have go too (certs: a marker left from another certificate would
	 mislead the app)
	:returns: what was written, as prefix/name"""
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
	written: list[str] = []
	for name in sorted(names):
		_write_like_folder(folder / name, zf.read(members[name]), folder)
		written.append(f"{prefix}/{name}")
	return written


def _restore_grafana(zf: zipfile.ZipFile, folder: Path) -> list[str]:
	"""Grafana's database into its data folder (Grafana stopped), without the
	journal files of the one it replaces.

	:returns: what was written"""
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
		owner_only(tmp)
	elif target.name == GRAFANA_MEMBER:
		# as Grafana keeps it: its owner, and group 0 reads (the app's backups)
		os.chmod(tmp, 0o640)
	if sys.platform != "win32" and os.geteuid() == 0:
		owner = folder.stat()
		os.chown(tmp, owner.st_uid, owner.st_gid)
	os.replace(tmp, target)
