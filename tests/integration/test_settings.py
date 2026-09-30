"""System settings store against real Postgres: precedence, validation,
all-or-nothing saves, reset."""
import pytest

from src.db.settings import SETTINGS, Setting, SettingsError
from src.db.tables import SystemSetting

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def store(app):
	return app.backend.settings


def rows(session_scope):
	with session_scope() as s:
		return {r.key: r.value for r in s.query(SystemSetting)}


def test_untouched_install_uses_defaults_and_stores_nothing(store, session_scope):
	assert store.get("job_retention_days") == 30
	assert store.values()["log_retention_days"] == SETTINGS["log_retention_days"].default
	assert rows(session_scope) == {}
	shown = {d["key"]: d for d in store.list_for_display()}
	assert shown["job_retention_days"]["source"] == "default"
	assert list(shown) == list(SETTINGS)          # registry order


def test_update_saves_only_changes_and_reports_them(store, make_user,
                                                    session_scope):
	admin = make_user(role="admin")
	changes = store.update({"job_retention_days": "45",
	                        "audit_retention_days": 90},   # unchanged
	                       admin.id)
	assert [(c.key, c.old, c.new) for c in changes] == [
		("job_retention_days", 30, 45)]
	assert rows(session_scope) == {"job_retention_days": 45}
	assert store.get("job_retention_days") == 45
	shown = {d["key"]: d for d in store.list_for_display()}
	assert shown["job_retention_days"]["source"] == "admin"
	with session_scope() as s:
		assert s.get(SystemSetting, "job_retention_days").updated_by == admin.id
	# changing it again updates the same row
	store.update({"job_retention_days": 50}, admin.id)
	assert rows(session_scope) == {"job_retention_days": 50}


def test_invalid_values_save_nothing(store, session_scope):
	with pytest.raises(SettingsError) as e:
		store.update({"job_retention_days": 45, "audit_retention_days": 3,
		              "no_such_setting": 1}, None)
	assert set(e.value.errors) == {"audit_retention_days", "no_such_setting"}
	assert "Audit log must be at least 7" in e.value.errors["audit_retention_days"]
	assert rows(session_scope) == {}              # all-or-nothing


def test_cross_setting_rules_check_the_combined_result(store, session_scope):
	# log retention (60) may not drop below job retention
	with pytest.raises(SettingsError) as e:
		store.update({"job_retention_days": 90}, None)
	assert "at least as long as job records" in e.value.errors[None]
	assert rows(session_scope) == {}
	# raising both together is fine
	store.update({"job_retention_days": 90, "log_retention_days": 120}, None)
	with pytest.raises(SettingsError, match="longer than their job record"):
		store.update({"config_snapshot_retention_days": 100}, None)


def test_reset_deletes_the_row_unless_that_breaks_a_rule(store, session_scope):
	store.update({"job_retention_days": 90, "log_retention_days": 120}, None)
	# resetting log retention would bring it to 60 < 90
	with pytest.raises(SettingsError, match="at least as long"):
		store.reset("log_retention_days")
	change = store.reset("job_retention_days")
	assert (change.old, change.new) == (90, 30)
	assert rows(session_scope) == {"log_retention_days": 120}
	assert store.reset("job_retention_days") is None     # already default


def test_install_value_between_admin_and_default(store, monkeypatch,
                                                 session_scope):
	# a setting with an install-time env var (B2 adds real ones)
	monkeypatch.setitem(SETTINGS, "test_workers", Setting(
		"test_workers", "Workers", "help", "Rollouts", 4, minimum=1,
		maximum=32, env="NR_TEST_WORKERS"))
	assert store.get("test_workers") == 4
	monkeypatch.setenv("NR_TEST_WORKERS", "8")
	assert store.get("test_workers") == 8
	shown = {d["key"]: d for d in store.list_for_display()}["test_workers"]
	assert (shown["source"], shown["install_value"]) == ("install", 8)
	store.update({"test_workers": 12}, None)
	assert store.get("test_workers") == 12         # admin beats install value
	store.reset("test_workers")
	assert store.get("test_workers") == 8          # back to the install value
	monkeypatch.setenv("NR_TEST_WORKERS", "999")   # out of range: ignored
	assert store.get("test_workers") == 4


def test_out_of_range_row_falls_back(store, session_scope):
	# e.g. a row written before a range was tightened
	with session_scope() as s:
		s.add(SystemSetting(key="audit_retention_days", value=1))
	assert store.get("audit_retention_days") == 90
