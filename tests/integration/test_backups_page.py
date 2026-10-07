"""System Settings → Backups: Back up now, the list, Download, Delete
(admins only, audited) and the scheduled backups with their retention —
against the test database, into a scratch backups folder."""
import zipfile
from datetime import datetime, timedelta

import pytest

from src import backup
from src.db.settings import seed_settings
from src.db.tables import AuditLog
from src.webapp import backup_schedule

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def home(tmp_path, monkeypatch):
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	return tmp_path


@pytest.fixture
def admin(make_user, session_scope):
	"""An admin, with every setting seeded first."""
	with session_scope() as s:
		seed_settings(s)
	return make_user(role="admin")


def actions(session_scope, prefix="backup."):
	"""(action, actor, success, label) of the audit entries starting with `prefix`,
	oldest first."""
	with session_scope() as s:
		return [(a.action, a.actor_username, a.success, a.object_label)
		        for a in s.query(AuditLog).order_by(AuditLog.timestamp)
		        if a.action.startswith(prefix)]


def test_back_up_now_list_download_delete(admin, home, client_for, session_scope,
                                          make_profile):
	"""Back up now makes a manual backup holding the database, lists it, downloads it as
	a zip and deletes it, each step audited."""
	make_profile(admin)
	client = client_for(admin, xhr=True)

	resp = client.post("/admin/backups")
	assert resp.status_code == 200, resp.json
	name = resp.json["created"]
	assert [b["name"] for b in resp.json["backups"]] == [name]
	assert resp.json["backups"][0]["kind"] == "manual"
	with zipfile.ZipFile(home / "backups" / name) as zf:
		assert "db/security_profiles.csv" in zf.namelist()

	download = client.get(f"/admin/backups/{name}")
	assert download.status_code == 200
	assert download.headers["Content-Disposition"].endswith(name)
	assert download.data[:2] == b"PK"
	download.close()        # the file stays open until then (Windows can't delete it)

	assert client.post(f"/admin/backups/{name}/delete").json["backups"] == []
	assert not (home / "backups" / name).exists()
	assert [a[0] for a in actions(session_scope)] == [
		"backup.created", "backup.downloaded", "backup.deleted"]


def test_only_a_backup_in_the_folder_by_its_exact_name(admin, home, client_for):
	"""Download and delete are 404 for a path outside the folder, a name that isn't a
	backup's and a backup that doesn't exist; the outside file stays."""
	client = client_for(admin, xhr=True)
	(home / "secret.txt").write_text("not a backup")
	for name in ("..%2Fsecret.txt", "secret.txt",
	             "netrollout-1.0.0-20261005-020000-manual.zip"):    # no such file
		assert client.get(f"/admin/backups/{name}").status_code == 404
		assert client.post(f"/admin/backups/{name}/delete").status_code == 404
	assert (home / "secret.txt").exists()


def test_operators_get_nothing(make_user, home, client_for):
	"""An operator gets 403 for the list and Back up now, and no backup is made."""
	client = client_for(make_user(role="operator"), xhr=True)
	assert client.get("/admin/backups").status_code == 403
	assert client.post("/admin/backups").status_code == 403
	assert not (home / "backups").exists()


def test_a_backup_while_another_runs_is_refused_and_audited(admin, home, client_for,
                                                            session_scope):
	"""Back up now while another backup or restore holds the lock is 409 and audited
	as failed."""
	(home / "backups").mkdir()
	(home / "backups" / backup.LOCK).write_text("")
	resp = client_for(admin, xhr=True).post("/admin/backups")
	assert resp.status_code == 409
	assert "Another backup or restore is running" in resp.json["message"]
	assert actions(session_scope) == [("backup.failed", admin.username, False, None)]


def test_the_scheduler_backs_up_once_per_time_and_keeps_the_newest(
		admin, app, home, session_scope):
	"""The scheduler backs up once per scheduled time (not again a minute later), keeps
	the newest `backup_keep`, records the last file and audits as `scheduler`."""
	app.backend.settings.update({"backup_keep": 2}, None)
	places = backup.Places.app()
	start = datetime(2026, 10, 5, 2, 0, 30)
	for day in range(3):
		now = start + timedelta(days=day)
		assert backup_schedule.tick(app.backend, now, places)["ok"]
		assert backup_schedule.tick(app.backend, now + timedelta(minutes=1), places) is None
	names = sorted(e.name for e in backup.list_backups(places.backups))
	assert [backup.NAME_RE.match(n)["stamp"][:8] for n in names] == ["20261006", "20261007"]
	assert backup_schedule.read_status(places.backups)["file"] == names[-1]
	created = [a for a in actions(session_scope) if a[0] == "backup.created"]
	assert len(created) == 3 and {a[1] for a in created} == {"scheduler"}


def test_scheduled_backups_off_make_none(admin, app, home):
	"""With the schedule off the scheduler makes no backup."""
	app.backend.settings.update({"backup_schedule": "off"}, None)
	assert backup_schedule.tick(app.backend, datetime(2026, 10, 5, 3, 0)) is None
	assert not backup.list_backups()


def test_a_failed_scheduled_backup_is_reported_audited_and_retried(
		admin, app, home, session_scope, monkeypatch, capsys):
	"""A failed scheduled backup is recorded, printed as ACTION NEEDED and audited; it
	isn't retried at the next check, only after an hour."""
	def broken(*a, **kw):
		raise backup.BackupError("disk full")
	monkeypatch.setattr(backup, "create", broken)
	now = datetime(2026, 10, 5, 2, 1)
	status = backup_schedule.tick(app.backend, now)
	assert status == {"time": "2026-10-05T02:01:00", "ok": False, "file": None,
	                  "message": "disk full"}
	assert "ACTION NEEDED — scheduled backup failed: disk full" in capsys.readouterr().out
	assert actions(session_scope) == [("backup.failed", "scheduler", False, None)]
	assert backup_schedule.tick(app.backend, now + timedelta(minutes=5)) is None
	monkeypatch.undo()
	monkeypatch.setenv("NETROLLOUT_HOME", str(home))
	assert backup_schedule.tick(app.backend, now + timedelta(minutes=61))["ok"]


def test_the_settings_page_shows_the_backups_card(admin, home, client_for):
	"""System Settings shows the Backups card: Back up now and the schedule dropdown
	(Daily selected)."""
	page = client_for(admin).get("/admin/settings").get_data(as_text=True)
	assert 'id="backupNowBtn"' in page
	assert '<select class="form-select form-select-sm set-input" id="set-backup_schedule"' in page
	assert '<option value="daily" selected>Daily</option>' in page
