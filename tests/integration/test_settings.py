"""System settings against real Postgres: seeding, reading the table,
all-or-nothing saves with the rules, reset."""
import pytest

from src import runtime
from src.db.settings import SETTINGS, Setting, SettingsError, seed_settings
from src.db.tables import SystemSetting
from src.webapp.proxy_config import sync_at_start

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def store(app, session_scope):
	"""The settings store, every setting seeded first (what install() does at every
	startup)."""
	with session_scope() as s:
		seed_settings(s)
	return app.backend.settings


def rows(session_scope):
	"""Every system_settings row as key -> (value, updated_by)."""
	with session_scope() as s:
		return {r.key: (r.value, r.updated_by) for r in s.query(SystemSetting)}


def test_seeding_fills_every_setting_once(app, session_scope):
	"""Seeding writes every setting at its default with no problems, and a later seeding
	never overwrites an existing row."""
	with session_scope() as s:
		assert seed_settings(s) == []
	seeded = rows(session_scope)
	assert set(seeded) == set(SETTINGS)
	assert {k: v for k, (v, _) in seeded.items()} == {
		k: s.default for k, s in SETTINGS.items()}
	# an existing row is never overwritten by a later startup
	app.backend.settings.update({"job_retention_days": 45}, None)
	with session_scope() as s:
		seed_settings(s)
	assert rows(session_scope)["job_retention_days"][0] == 45


def test_seeding_takes_the_install_value(monkeypatch, session_scope, app):
	"""Seeding takes a setting's install value from its env var (shown as changed, from
	install); after that the env var is never consulted again."""
	monkeypatch.setitem(SETTINGS, "test_workers", Setting(
		"test_workers", "Workers", "help", "Rollouts", 4, minimum=1,
		maximum=32, env="NR_TEST_WORKERS"))
	monkeypatch.setenv("NR_TEST_WORKERS", "8")
	with session_scope() as s:
		seed_settings(s)
	store = app.backend.settings
	assert store.get("test_workers") == 8
	shown = {d["key"]: d for d in store.list_for_display()}["test_workers"]
	assert shown["changed"] and shown["from_install"]
	# after install the env is never consulted again
	monkeypatch.setenv("NR_TEST_WORKERS", "16")
	assert store.get("test_workers") == 8


def test_reading_is_the_table(store, session_scope):
	"""A setting reads its row in the table, so a change there shows at once; the display
	list marks changed settings and keeps the registry order."""
	assert store.get("job_retention_days") == 30
	with session_scope() as s:
		s.get(SystemSetting, "job_retention_days").value = 44
	assert store.get("job_retention_days") == 44
	shown = {d["key"]: d for d in store.list_for_display()}
	assert shown["job_retention_days"]["changed"]
	assert not shown["audit_retention_days"]["changed"]
	assert list(shown) == list(SETTINGS)          # registry order


def test_update_saves_only_changes_and_reports_them(store, make_user,
                                                    session_scope):
	"""Update saves only the changed values with who changed them and returns those
	changes; an unchanged value keeps its row."""
	admin = make_user(role="admin")
	changes = store.update({"job_retention_days": "45",
	                        "audit_retention_days": 90},   # unchanged
	                       admin.id)
	assert [(c.key, c.old, c.new) for c in changes] == [
		("job_retention_days", 30, 45)]
	assert rows(session_scope)["job_retention_days"] == (45, admin.id)
	assert rows(session_scope)["audit_retention_days"] == (90, None)
	shown = {d["key"]: d for d in store.list_for_display()}["job_retention_days"]
	assert shown["changed"] and not shown["from_install"]


def test_invalid_values_save_nothing(store, session_scope):
	"""Invalid or unknown keys raise SettingsError with an error per key, and nothing is
	saved (all or nothing)."""
	before = rows(session_scope)
	with pytest.raises(SettingsError) as e:
		store.update({"job_retention_days": 45, "audit_retention_days": 3,
		              "no_such_setting": 1}, None)
	assert set(e.value.errors) == {"audit_retention_days", "no_such_setting"}
	assert "Audit log must be at least 7" in e.value.errors["audit_retention_days"]
	assert rows(session_scope) == before          # all-or-nothing


def test_rules_check_the_combined_result(store, session_scope):
	"""Rules check the values after the update: job records longer than logs is refused
	(nothing saved), both together pass; config snapshots can't outlive job records."""
	before = rows(session_scope)
	with pytest.raises(SettingsError) as e:        # log (60) < job (90)
		store.update({"job_retention_days": 90}, None)
	assert "at least as long as job records" in e.value.errors[None]
	assert rows(session_scope) == before
	store.update({"job_retention_days": 90, "log_retention_days": 120}, None)
	with pytest.raises(SettingsError, match="longer than their job record"):
		store.update({"config_snapshot_retention_days": 100}, None)


def test_reset_writes_the_default_unless_that_breaks_a_rule(store, make_user,
                                                           session_scope):
	"""Reset writes the default and returns the change; a reset that breaks a rule is
	refused, and resetting a default returns None."""
	admin = make_user(role="admin")
	store.update({"job_retention_days": 90, "log_retention_days": 120}, admin.id)
	with pytest.raises(SettingsError, match="at least as long"):
		store.reset("log_retention_days", admin.id)   # 60 < 90
	change = store.reset("job_retention_days", admin.id)
	assert (change.old, change.new) == (90, 30)
	assert rows(session_scope)["job_retention_days"] == (30, admin.id)
	assert store.reset("job_retention_days", admin.id) is None  # already default


def test_out_of_range_row_is_used_as_nearest_valid_and_reported(store,
                                                                session_scope):
	"""A stored value out of range (e.g. written before a range was tightened) reads as
	the nearest valid value, and seeding reports it."""
	with session_scope() as s:
		s.get(SystemSetting, "audit_retention_days").value = 1
	assert store.get("audit_retention_days") == 7
	with session_scope() as s:
		problems = seed_settings(s)
	assert problems == ["setting audit_retention_days: stored 1 must be at "
	                    "least 7 — using 7"]


def test_missing_row_falls_back_to_the_default(app, session_scope):
	"""With no rows (only if seeding failed) a setting reads as its default: reading a
	setting never crashes."""
	assert rows(session_scope) == {}
	assert app.backend.settings.get("job_retention_days") == 30


def test_the_app_hands_nginx_the_saved_hostname_at_start(app):
	"""site.env starts with the hostname line, and a start writes the saved hostname
	into it."""
	# what the nginx watcher reads (config/nginx/site.env in NETROLLOUT_HOME)
	site = runtime.config_dir() / "nginx" / "site.env"
	assert site.read_text(encoding="utf-8").startswith("NETROLLOUT_HOSTNAME=")
	app.backend.settings.update({"public_hostname": "nr01.corp.local"}, None)
	try:
		sync_at_start(app.backend.settings)          # what the next start does
		assert "NETROLLOUT_HOSTNAME=nr01.corp.local\n" in site.read_text(encoding="utf-8")
	finally:
		app.backend.settings.update({"public_hostname": ""}, None)
		sync_at_start(app.backend.settings)
