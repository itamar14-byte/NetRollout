"""System settings registry (src/db/settings.py): definitions, parsing,
coercion of stored values, seed values and the declarative rules — no
database here (the store and seeding are tested in integration)."""
import pytest

from src.db.settings import (RULES, SETTINGS, Rule, Setting, rules_for_client,
                             sql_value)


def test_defaults_satisfy_every_rule_and_range():
	"""The defaults satisfy every rule, and every int default parses to itself."""
	defaults = {k: s.default for k, s in SETTINGS.items()}
	for rule in RULES:
		assert rule.holds(defaults), rule.message
	for s in SETTINGS.values():
		if s.kind is int and s.default is not None:
			assert s.parse(s.default) == s.default, s.key


def test_settings_read_by_sql_have_no_install_value():
	"""A setting read by SQL has no install env var: the SQL reads only the table row,
	never the app's environment."""
	for s in SETTINGS.values():
		if s.sql:
			assert s.env is None, s.key


def test_every_setting_is_documented():
	"""Every setting has a label, help, card and an "applies" text."""
	for s in SETTINGS.values():
		assert s.label and s.help and s.card and s.applies, s.key


@pytest.mark.parametrize("raw,expected", [
	(30, 30), ("30", 30), (" 45 ", 45), (1, 1), (3650, 3650),
])
def test_int_parse(raw, expected):
	"""An int setting parses ints and numeric strings (trimmed) within its range."""
	assert SETTINGS["job_retention_days"].parse(raw) == expected


@pytest.mark.parametrize("raw,message", [
	(0, "must be at least 1"), (3651, "must be at most 3650"),
	("abc", "must be a whole number"), ("", "must be a whole number"),
	(True, "must be a whole number"), (None, "must be a whole number"),
	("7.5", "must be a whole number"),
])
def test_int_parse_rejects(raw, message):
	"""An int setting refuses values out of range, non-numbers, blanks, booleans, None
	and decimals, with the matching message."""
	with pytest.raises(ValueError, match=message):
		SETTINGS["job_retention_days"].parse(raw)


def test_audit_log_has_a_higher_floor():
	"""The audit log retention refuses fewer than 7 days."""
	with pytest.raises(ValueError, match="at least 7"):
		SETTINGS["audit_retention_days"].parse(6)


@pytest.mark.parametrize("stored,value,problem", [
	(45, 45, False),
	(3, 7, True),          # below a (tightened) minimum → nearest valid
	(99999, 3650, True),   # above the maximum
	("garbage", 90, True), # unreadable → default
	(None, 90, True),
])
def test_coerce_never_raises(stored, value, problem):
	"""Coercing a stored value never raises: a valid one is kept, out of range becomes
	the nearest bound, unreadable or None the default; all but the valid one report
	a problem."""
	got, why = SETTINGS["audit_retention_days"].coerce(stored)
	assert got == value and bool(why) is problem


def test_seed_value_prefers_a_valid_install_value(monkeypatch):
	"""The seed value is the env var's when set and valid, else the default."""
	s = Setting("w", "W", "help", "Rollouts", 4, minimum=1, maximum=32,
	            env="NR_TEST_W")
	monkeypatch.delenv("NR_TEST_W", raising=False)
	assert s.seed_value() == 4
	monkeypatch.setenv("NR_TEST_W", "8")
	assert s.seed_value() == 8
	monkeypatch.setenv("NR_TEST_W", "99")      # out of range: default
	assert s.seed_value() == 4


def test_rules_are_declarative_and_shared_with_the_page():
	"""The two rules (logs and config snapshots against job records) fail when broken,
	and the page gets exactly these rules as data."""
	values = {k: s.default for k, s in SETTINGS.items()}
	log_rule, snap_rule = RULES
	assert not log_rule.holds({**values, "log_retention_days": 10,
	                           "job_retention_days": 30})
	assert not snap_rule.holds({**values, "config_snapshot_retention_days": 40,
	                            "job_retention_days": 30})
	# the browser gets exactly these, as data
	assert rules_for_client() == [
		{"left": r.left, "op": r.op, "right": r.right, "message": r.message}
		for r in RULES]
	assert Rule("a", ">=", "b", "m").holds({"a": 2, "b": 2})


def test_sql_value_reads_the_row():
	"""sql_value reads the setting's row with the default as a fallback; a setting not
	read by SQL is refused."""
	sql = sql_value("job_retention_days")
	assert "WHERE key = 'job_retention_days'" in sql
	assert sql.endswith(", 30)")          # safety net only
	with pytest.raises(AssertionError):   # not read by SQL
		sql_value("log_retention_days")


def test_str_setting_parse_trims():
	"""A str setting trims its value, and None becomes empty."""
	s = Setting("x", "X", "help", "Access", None, kind=str)
	assert s.parse("  https://nr.corp  ") == "https://nr.corp"
	assert s.parse(None) == ""
