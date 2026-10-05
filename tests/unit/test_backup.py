"""The backup files without a database: what a restore refuses before it
touches anything, the list, retention, the one-at-a-time lock, which files a
restore writes."""
import json
import os
import time
import zipfile
from dataclasses import asdict

import pytest

from src import backup

HEAD_KNOWN = "v1_0_0_baseline"


def make_zip(folder, name="netrollout-1.0.0-20261005-020000-scheduled.zip", *,
             version="1.0.0", revision=HEAD_KNOWN, tables=None, members=None,
             grafana=False):
	tables = {"users": 1} if tables is None else tables
	manifest = backup.Manifest(backup.FORMAT, version, "2026-10-05T02:00:00",
	                           name.rsplit("-", 1)[1].removesuffix(".zip"),
	                           revision, tables, grafana, [], 0)
	path = folder / name
	with zipfile.ZipFile(path, "w") as zf:
		zf.writestr(backup.MANIFEST, json.dumps(asdict(manifest)))
		for member, data in (members if members is not None else
		                     {**{f"db/{t}.csv": "id\n" for t in tables},
		                      backup.KEY_MEMBER: "k"}).items():
			zf.writestr(member, data)
	return path


# ── check: refused before anything is touched ────────────────────────────────

def test_a_backup_from_this_or_an_older_version_is_accepted(tmp_path):
	assert backup.check(make_zip(tmp_path, version="1.0.0"), "1.0.1").version == "1.0.0"
	assert backup.check(make_zip(tmp_path, version="1.0.0"), "1.0.0")


def test_a_backup_from_a_newer_version_is_refused(tmp_path):
	with pytest.raises(backup.BackupError, match="newer than this one"):
		backup.check(make_zip(tmp_path, version="1.1.0"), "1.0.0")
	# a release is newer than its own development build
	with pytest.raises(backup.BackupError, match="newer"):
		backup.check(make_zip(tmp_path, version="1.0.0"), "1.0.0.dev0")


def test_a_database_level_this_version_doesnt_know_is_refused(tmp_path):
	with pytest.raises(backup.BackupError, match="doesn't know"):
		backup.check(make_zip(tmp_path, revision="from_the_future"), "9.9.9")


def test_an_incomplete_backup_is_refused(tmp_path):
	path = make_zip(tmp_path, tables={"users": 1, "audit_log": 3},
	                members={"db/users.csv": "id\n"})
	with pytest.raises(backup.BackupError, match="missing db/audit_log.csv, encryption.key"):
		backup.check(path, "1.0.0")
	path = make_zip(tmp_path, grafana=True)
	with pytest.raises(backup.BackupError, match="missing grafana.db"):
		backup.check(path, "1.0.0")


def test_a_file_that_isnt_a_backup_is_refused(tmp_path):
	path = tmp_path / "netrollout-1.0.0-20261005-020000-manual.zip"
	path.write_text("not a zip")
	with pytest.raises(backup.BackupError, match="isn't a NetRollout backup"):
		backup.check(path, "1.0.0")
	with pytest.raises(backup.BackupError, match="doesn't exist"):
		backup.check(tmp_path / "gone.zip", "1.0.0")


# ── the list and retention ───────────────────────────────────────────────────

def test_the_list_is_newest_first_and_only_netrollout_backups(tmp_path):
	make_zip(tmp_path, "netrollout-1.0.0-20261003-020000-scheduled.zip")
	make_zip(tmp_path, "netrollout-1.0.0-20261005-091500-manual.zip")
	(tmp_path / "netrollout-1.0.0-20261004-020000-scheduled.zip").write_text("bad")
	(tmp_path / "netrollout_2026-10-04.dump").write_text("someone else's")
	(tmp_path / ".netrollout-1.0.0-20261006-020000-manual.zip.partial").write_text("")

	entries = backup.list_backups(tmp_path)
	assert [e.name[17:32] for e in entries] == [
		"20261005-091500", "20261004-020000", "20261003-020000"]
	assert entries[0].manifest.kind == "manual"
	assert entries[1].manifest is None and "isn't a NetRollout backup" in entries[1].problem


def test_retention_deletes_only_the_oldest_scheduled_backups(tmp_path):
	for day in ("01", "02", "03", "04"):
		make_zip(tmp_path, f"netrollout-1.0.0-202610{day}-020000-scheduled.zip")
	make_zip(tmp_path, "netrollout-1.0.0-20260930-120000-manual.zip")
	make_zip(tmp_path, "netrollout-1.0.0-20260929-120000-before-restore.zip")

	gone = backup.prune(tmp_path, keep=2)
	assert sorted(p.name[17:25] for p in gone) == ["20261001", "20261002"]
	left = sorted(p.name for p in tmp_path.iterdir())
	assert left == ["netrollout-1.0.0-20260929-120000-before-restore.zip",
	                "netrollout-1.0.0-20260930-120000-manual.zip",
	                "netrollout-1.0.0-20261003-020000-scheduled.zip",
	                "netrollout-1.0.0-20261004-020000-scheduled.zip"]


# ── one at a time ────────────────────────────────────────────────────────────

def test_a_second_backup_waits_its_turn_and_a_crashed_ones_lock_expires(tmp_path):
	with backup._lock(tmp_path):
		with pytest.raises(backup.BackupError, match="Another backup or restore"):
			with backup._lock(tmp_path):
				pass
	assert not (tmp_path / backup.LOCK).exists()

	(tmp_path / backup.LOCK).write_text("")
	old = time.time() - backup.STALE_LOCK_SECONDS - 60
	os.utime(tmp_path / backup.LOCK, (old, old))
	with backup._lock(tmp_path):
		pass


# ── which files a restore writes ─────────────────────────────────────────────

def zip_with(tmp_path, files):
	path = tmp_path / "files.zip"
	with zipfile.ZipFile(path, "w") as zf:
		for member, data in files.items():
			zf.writestr(member, data)
	return zipfile.ZipFile(path)


def test_the_certificate_comes_with_its_markers_and_nothing_else(tmp_path):
	certs = tmp_path / "certs"
	certs.mkdir()
	(certs / "fullchain.pem").write_text("self-signed")
	(certs / ".selfsigned").write_text("")
	(certs / "README").write_text("not ours")
	# the backup's certificate is an organisation's: no self-signed marker
	with zip_with(tmp_path, {"certs/fullchain.pem": "org", "certs/privkey.pem": "k",
	                         "certs/notes.txt": "ignored"}) as zf:
		backup._restore_files(zf, "certs", certs, set(backup.CERT_FILES),
		                      replace_all=True)
	assert (certs / "fullchain.pem").read_text() == "org"
	assert not (certs / ".selfsigned").exists()
	assert not (certs / "notes.txt").exists()
	assert (certs / "README").exists()


def test_a_backup_without_a_certificate_keeps_this_installs(tmp_path):
	certs = tmp_path / "certs"
	certs.mkdir()
	(certs / "fullchain.pem").write_text("current")
	with zip_with(tmp_path, {"logs/rollout_a.log": "x"}) as zf:
		assert backup._restore_files(zf, "certs", certs, set(backup.CERT_FILES),
		                             replace_all=True) == []
	assert (certs / "fullchain.pem").read_text() == "current"


def test_only_rollout_logs_are_restored_and_never_outside_the_folder(tmp_path):
	logs = tmp_path / "logs"
	with zip_with(tmp_path, {"logs/rollout_x_job.log": "a",
	                         "logs/unsaved-results-job.json": "{}",
	                         "logs/install.log": "no",
	                         "logs/../../rollout_escape.log": "b"}) as zf:
		written = backup._restore_files(zf, "logs", logs, None)
	assert sorted(p.name for p in logs.iterdir()) == [
		"rollout_escape.log", "rollout_x_job.log", "unsaved-results-job.json"]
	assert not (tmp_path / "rollout_escape.log").exists()
	assert "logs/install.log" not in written
