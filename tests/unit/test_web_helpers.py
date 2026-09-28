"""Pure webapp helpers: query allowlist, device access rules, KPIs, job
status, snapshot expiry, mapping-field validation. No app, no DB."""
import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src.db.db_install import CONFIG_SNAPSHOT_RETENTION_DAYS
from src.validation import Validator
from src.webapp.blueprints.admin_observability import QUERY_AUDIT_LOG_FIELDS
from src.webapp.blueprints.analytics import QUERY_DEVICE_RESULT_FIELDS
from src.webapp.blueprints.jobs import config_expired, job_status
from src.webapp.utils import (build_kpi, can_edit_device, compile_query_rules,
                              partition_devices, visible_devices_clause)


def sql(expr) -> str:
	return str(expr.compile(dialect=postgresql.dialect(),
	                        compile_kwargs={"literal_binds": True}))


def leaf(field, operator, value):
	return {"field": field, "operator": operator, "value": value}


# ── compile_query_rules: the QueryBuilder -> SQL allowlist ───────────────────

class TestCompileQueryRules:

	def test_allowed_leaf_compiles(self):
		expr = compile_query_rules(leaf("status", "equal", "failed"),
		                           QUERY_DEVICE_RESULT_FIELDS)
		assert sql(expr) == "device_results.status = 'failed'"

	def test_unknown_field_rejected(self):
		with pytest.raises(ValueError, match="Field not allowed"):
			compile_query_rules(leaf("fetched_config", "contains", "x"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_operator_not_allowed_for_field_rejected(self):
		with pytest.raises(ValueError, match="not allowed"):
			compile_query_rules(leaf("status", "contains", "fail"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_audit_fields_not_queryable_from_device_results(self):
		# each surface has its own allowlist — no cross-table querying
		with pytest.raises(ValueError, match="Field not allowed"):
			compile_query_rules(leaf("actor_username", "equal", "admin"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_values_are_bound_not_interpolated(self):
		payload = "x' OR '1'='1"
		compiled = compile_query_rules(
			leaf("device_ip", "contains", payload),
			QUERY_DEVICE_RESULT_FIELDS).compile(dialect=postgresql.dialect())
		# the SQL text holds only a placeholder; the value travels separately
		assert payload not in str(compiled)
		assert "%(device_ip_1)s" in str(compiled)
		assert compiled.params["device_ip_1"] == f"%{payload}%"

	def test_nested_groups_combine(self):
		tree = {"condition": "AND", "rules": [
			leaf("device_type", "equal", "cisco_ios"),
			{"condition": "OR", "rules": [
				leaf("status", "equal", "failed"),
				leaf("status", "equal", "partial")]}]}
		out = sql(compile_query_rules(tree, QUERY_DEVICE_RESULT_FIELDS))
		assert " AND " in out and " OR " in out

	def test_date_values_parsed(self):
		out = sql(compile_query_rules(
			leaf("started_at", "greater_or_equal", "2026-09-01"),
			QUERY_DEVICE_RESULT_FIELDS))
		assert "2026-09-01" in out

	def test_invalid_date_rejected(self):
		with pytest.raises(ValueError, match="Invalid date"):
			compile_query_rules(leaf("started_at", "equal", "yesterday"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_boolean_strings_coerced(self):
		out = sql(compile_query_rules(leaf("success", "equal", "false"),
		                              QUERY_AUDIT_LOG_FIELDS))
		assert out == "audit_log.success = false"


# ── Global device access rules ───────────────────────────────────────────────

OWNER, OTHER = uuid.uuid4(), uuid.uuid4()


def device(user_id, is_global):
	return SimpleNamespace(user_id=user_id, is_global=is_global)


def user(user_id, role="user"):
	return SimpleNamespace(id=user_id, role=role)


class TestDeviceAccess:

	def test_owner_edits_own_local_device(self):
		assert can_edit_device(device(OWNER, False), user(OWNER))

	def test_other_user_cannot_edit_local_device(self):
		assert not can_edit_device(device(OWNER, False), user(OTHER))

	def test_admin_cannot_edit_someone_elses_local_device(self):
		assert not can_edit_device(device(OWNER, False), user(OTHER, "admin"))

	def test_any_admin_edits_global_device(self):
		assert can_edit_device(device(OWNER, True), user(OTHER, "admin"))

	def test_regular_user_cannot_edit_global_device(self):
		assert not can_edit_device(device(OWNER, True), user(OTHER))

	def test_partition_splits_global_and_local(self):
		g, m = device(OWNER, True), device(OTHER, False)
		assert partition_devices([m, g]) == ([g], [m])

	def test_visibility_is_own_or_global(self):
		out = sql(visible_devices_clause(OWNER))
		assert "inventory.user_id" in out and "inventory.is_global IS true" in out
		assert " OR " in out


# ── KPIs, job status, snapshot expiry ────────────────────────────────────────

def result(status, ip="10.0.0.1", job_id=None, sent=2, verified=None,
           config=None, age_days=0):
	return SimpleNamespace(
		status=status, device_ip=ip, job_id=job_id or uuid.uuid4(),
		commands_sent=sent, commands_verified=verified, fetched_config=config,
		completed_at=dt.datetime.now() - dt.timedelta(days=age_days))


def test_build_kpi():
	job = uuid.uuid4()
	rows = [result("success", job_id=job), result("failed", "10.0.0.9", job),
	        result("failed", "10.0.0.9"), result("partial")]
	kpi = build_kpi(rows, {"10.0.0.9": "edge-9"})
	assert kpi["success_rate"] == 25
	assert kpi["jobs_30d"] == 3
	assert kpi["devices_reached"] == 4
	assert kpi["commands_pushed"] == 8
	assert kpi["top_failed"] == {"ip": "10.0.0.9", "label": "edge-9",
	                             "fail_count": 2}


def test_build_kpi_empty():
	kpi = build_kpi([], {})
	assert kpi["success_rate"] is None and kpi["top_failed"] is None


@pytest.mark.parametrize("statuses,expected", [
	(["success", "success"], "success"),
	(["success", "failed"], "partial"),
	(["success", "partial"], "partial"),
	(["failed", "failed"], "failed"),
	(["success", "cancelled"], "cancelled"),
])
def test_job_status(statuses, expected):
	assert job_status([result(s) for s in statuses]) == expected


class TestConfigExpired:
	OLD = CONFIG_SNAPSHOT_RETENTION_DAYS + 1

	def test_mismatch_past_window_without_config_is_expired(self):
		assert config_expired(result("partial", verified=1, age_days=self.OLD))

	def test_within_window_is_not_expired(self):
		assert not config_expired(result("partial", verified=1, age_days=1))

	def test_config_still_present_is_not_expired(self):
		assert not config_expired(
			result("partial", verified=1, config="cfg", age_days=self.OLD))

	def test_fully_verified_never_had_a_snapshot(self):
		assert not config_expired(result("success", verified=2, age_days=self.OLD))

	def test_verify_not_run_never_had_a_snapshot(self):
		assert not config_expired(result("success", verified=None, age_days=self.OLD))


# ── Variable-mapping field validation ────────────────────────────────────────

@pytest.mark.parametrize("token,ok", [
	("HOSTNAME", True), ("loop_0", True), ("", False), ("   ", False),
	("has space", False), ("$$X$$", False), ("A" * 65, False)])
def test_mapping_token_validation(token, ok):
	assert Validator.validate_var_map_inner_token(token)[0] is ok


def test_mapping_index_only_for_vrfs():
	assert Validator.validate_var_index(1, "vrfs") == (True, None)
	assert Validator.validate_var_index(None, "hostname") == (True, None)
	assert Validator.validate_var_index(0, "hostname")[0] is False
	assert Validator.validate_var_index(-1, "vrfs")[0] is False
