"""System settings registry (src/db/settings.py): definitions, parsing and
rules — no database here (the store is tested in integration)."""
import pytest

from src.db.settings import RULES, SETTINGS, Setting, sql_value


def test_defaults_satisfy_every_rule_and_range():
	defaults = {k: s.default for k, s in SETTINGS.items()}
	for keys, check, message in RULES:
		assert check(defaults), message
	for s in SETTINGS.values():
		if s.kind is int and s.default is not None:
			assert s.parse(s.default) == s.default, s.key


def test_settings_read_by_sql_have_no_install_value():
	# pg_cron runs inside Postgres, where the app's environment doesn't exist
	for s in SETTINGS.values():
		if s.sql:
			assert s.env is None, s.key


def test_every_setting_is_documented():
	for s in SETTINGS.values():
		assert s.label and s.help and s.card and s.applies, s.key


@pytest.mark.parametrize("raw,expected", [
	(30, 30), ("30", 30), (" 45 ", 45), (1, 1), (3650, 3650),
])
def test_int_parse(raw, expected):
	assert SETTINGS["job_retention_days"].parse(raw) == expected


@pytest.mark.parametrize("raw,message", [
	(0, "must be at least 1"), (3651, "must be at most 3650"),
	("abc", "must be a whole number"), ("", "must be a whole number"),
	(True, "must be a whole number"), (None, "must be a whole number"),
	("7.5", "must be a whole number"),
])
def test_int_parse_rejects(raw, message):
	with pytest.raises(ValueError, match=message):
		SETTINGS["job_retention_days"].parse(raw)


def test_audit_log_has_a_higher_floor():
	with pytest.raises(ValueError, match="at least 7"):
		SETTINGS["audit_retention_days"].parse(6)


def test_rules():
	values = {k: s.default for k, s in SETTINGS.items()}
	(_, log_rule, _), (_, snap_rule, _) = RULES
	assert not log_rule({**values, "log_retention_days": 10,
	                     "job_retention_days": 30})
	assert not snap_rule({**values, "config_snapshot_retention_days": 40,
	                      "job_retention_days": 30})


def test_sql_value_falls_back_to_the_default():
	sql = sql_value("job_retention_days")
	assert "WHERE key = 'job_retention_days'" in sql
	assert sql.endswith(", 30)")
	with pytest.raises(AssertionError):   # not read by SQL
		sql_value("log_retention_days")


def test_str_setting_parse_trims():
	s = Setting("x", "X", "help", "Access", None, kind=str)
	assert s.parse("  https://nr.corp  ") == "https://nr.corp"
	assert s.parse(None) == ""
