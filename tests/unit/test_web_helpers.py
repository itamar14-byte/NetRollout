"""Pure webapp helpers: query allowlist, device access rules, KPIs, job
status, snapshot expiry, mapping-field validation. No app, no DB."""
import datetime as dt
import sys
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src import validation
from src.db.settings import SETTINGS
from src.webapp.blueprints.admin_observability import QUERY_AUDIT_LOG_FIELDS
from src.webapp.blueprints.analytics import QUERY_DEVICE_RESULT_FIELDS
from src.webapp.blueprints.jobs import config_expired, job_status
from src.webapp.lifecycle import relaunch_command
from src.webapp.utils import (build_kpi, can_edit_device, compile_query_rules,
                              partition_devices, visible_devices_clause)


def sql(expr) -> str:
	"""The expression as PostgreSQL SQL text, values inlined."""
	return str(expr.compile(dialect=postgresql.dialect(),
	                        compile_kwargs={"literal_binds": True}))


def leaf(field, operator, value):
	return {"field": field, "operator": operator, "value": value}


# ── compile_query_rules: the QueryBuilder -> SQL allowlist ───────────────────

class TestCompileQueryRules:

	def test_allowed_leaf_compiles(self):
		"""An allowed field and operator compile to `device_results.status = 'failed'`."""
		expr = compile_query_rules(leaf("status", "equal", "failed"),
		                           QUERY_DEVICE_RESULT_FIELDS)
		assert sql(expr) == "device_results.status = 'failed'"

	def test_unknown_field_rejected(self):
		"""A field outside the allowlist (fetched_config) raises "Field not allowed"."""
		with pytest.raises(ValueError, match="Field not allowed"):
			compile_query_rules(leaf("fetched_config", "contains", "x"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_operator_not_allowed_for_field_rejected(self):
		"""An allowed field with an operator it doesn't allow (status contains) is
		refused with "not allowed"."""
		with pytest.raises(ValueError, match="not allowed"):
			compile_query_rules(leaf("status", "contains", "fail"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_audit_fields_not_queryable_from_device_results(self):
		"""Each surface has its own allowlist - no cross-table querying: an audit log
		field (actor_username) is refused on the device results allowlist."""
		with pytest.raises(ValueError, match="Field not allowed"):
			compile_query_rules(leaf("actor_username", "equal", "admin"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_values_are_bound_not_interpolated(self):
		"""An SQL injection payload never reaches the SQL text: the text holds only
		a placeholder and the value (wrapped in % for contains) travels as a
		parameter."""
		payload = "x' OR '1'='1"
		compiled = compile_query_rules(
			leaf("device_ip", "contains", payload),
			QUERY_DEVICE_RESULT_FIELDS).compile(dialect=postgresql.dialect())
		assert payload not in str(compiled)
		assert "%(device_ip_1)s" in str(compiled)
		assert compiled.params["device_ip_1"] == f"%{payload}%"

	def test_nested_groups_combine(self):
		"""An AND group holding an OR group compiles to SQL with both AND and OR."""
		tree = {"condition": "AND", "rules": [
			leaf("device_type", "equal", "cisco_ios"),
			{"condition": "OR", "rules": [
				leaf("status", "equal", "failed"),
				leaf("status", "equal", "partial")]}]}
		out = sql(compile_query_rules(tree, QUERY_DEVICE_RESULT_FIELDS))
		assert " AND " in out and " OR " in out

	def test_date_values_parsed(self):
		"""A date string on a date field is accepted and appears in the SQL."""
		out = sql(compile_query_rules(
			leaf("started_at", "greater_or_equal", "2026-09-01"),
			QUERY_DEVICE_RESULT_FIELDS))
		assert "2026-09-01" in out

	def test_invalid_date_rejected(self):
		"""A value that isn't a date ("yesterday") on a date field raises "Invalid date"."""
		with pytest.raises(ValueError, match="Invalid date"):
			compile_query_rules(leaf("started_at", "equal", "yesterday"),
			                    QUERY_DEVICE_RESULT_FIELDS)

	def test_boolean_strings_coerced(self):
		"""The string "false" on a boolean field becomes the SQL boolean false."""
		out = sql(compile_query_rules(leaf("success", "equal", "false"),
		                              QUERY_AUDIT_LOG_FIELDS))
		assert out == "audit_log.success = false"


# ── Global device access rules ───────────────────────────────────────────────

OWNER, OTHER = uuid.uuid4(), uuid.uuid4()


def device(user_id, is_global):
	return SimpleNamespace(user_id=user_id, is_global=is_global)


def user(user_id, role="operator"):
	return SimpleNamespace(id=user_id, role=role)


class TestDeviceAccess:

	def test_owner_edits_own_local_device(self):
		"""The owner may edit their own local device."""
		assert can_edit_device(device(OWNER, False), user(OWNER))

	def test_other_user_cannot_edit_local_device(self):
		"""Another operator may not edit someone's local device."""
		assert not can_edit_device(device(OWNER, False), user(OTHER))

	def test_admin_cannot_edit_someone_elses_local_device(self):
		"""Being an admin doesn't allow editing another user's local device."""
		assert not can_edit_device(device(OWNER, False), user(OTHER, "admin"))

	def test_any_admin_edits_global_device(self):
		"""Any admin, not only its owner, may edit a global device."""
		assert can_edit_device(device(OWNER, True), user(OTHER, "admin"))

	def test_regular_user_cannot_edit_global_device(self):
		"""An operator who doesn't own a global device may not edit it."""
		assert not can_edit_device(device(OWNER, True), user(OTHER))

	def test_partition_splits_global_and_local(self):
		"""partition_devices returns (global devices, local devices)."""
		g, m = device(OWNER, True), device(OTHER, False)
		assert partition_devices([m, g]) == ([g], [m])

	def test_visibility_is_own_or_global(self):
		"""The visibility filter is the user's own devices OR the global ones."""
		out = sql(visible_devices_clause(OWNER))
		assert "inventory.user_id" in out and "inventory.is_global IS true" in out
		assert " OR " in out


# ── KPIs, job status, snapshot expiry ────────────────────────────────────────

def result(status, ip="10.0.0.1", job_id=None, sent=2, verified=None,
           config=None, age_days=0):
	"""A stand-in device result row: its own job unless one is given,
	completed age_days ago."""
	return SimpleNamespace(
		status=status, device_ip=ip, job_id=job_id or uuid.uuid4(),
		commands_sent=sent, commands_verified=verified, fetched_config=config,
		completed_at=dt.datetime.now() - dt.timedelta(days=age_days))


def test_build_kpi():
	"""Four results (1 success) over three jobs: success rate 25 %, 3 jobs, 4
	devices reached (one per result), 8 commands pushed, and the device that
	failed twice named by its label as the top failed."""
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
	"""Without results the success rate and the top failed device are None."""
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
	"""A job's status from its devices': all success → success, any failed or
	partial → partial, all failed → failed, any cancelled → cancelled."""
	assert job_status([result(s) for s in statuses]) == expected


class TestConfigExpired:
	DAYS = SETTINGS["config_snapshot_retention_days"].default
	OLD = DAYS + 1

	def test_mismatch_past_window_without_config_is_expired(self):
		"""A verify mismatch older than the snapshot retention, with no config
		left, is reported as expired."""
		assert config_expired(result("partial", verified=1, age_days=self.OLD),
		                      self.DAYS)

	def test_within_window_is_not_expired(self):
		"""A verify mismatch inside the retention window isn't expired."""
		assert not config_expired(result("partial", verified=1, age_days=1),
		                          self.DAYS)

	def test_window_follows_the_setting(self):
		"""The same 3-day-old row is not expired with a 7-day retention and is
		expired with a 2-day one."""
		row = result("partial", verified=1, age_days=3)
		assert not config_expired(row, 7)
		assert config_expired(row, 2)

	def test_config_still_present_is_not_expired(self):
		"""An old mismatch whose config snapshot is still stored isn't expired."""
		assert not config_expired(
			result("partial", verified=1, config="cfg", age_days=self.OLD),
			self.DAYS)

	def test_fully_verified_never_had_a_snapshot(self):
		"""An old fully verified result never had a snapshot, so it isn't expired."""
		assert not config_expired(
			result("success", verified=2, age_days=self.OLD), self.DAYS)

	def test_verify_not_run_never_had_a_snapshot(self):
		"""An old result where verify didn't run never had a snapshot: not expired."""
		assert not config_expired(
			result("success", verified=None, age_days=self.OLD), self.DAYS)


# ── Variable-mapping field validation ────────────────────────────────────────

@pytest.mark.parametrize("token,ok", [
	("HOSTNAME", True), ("loop_0", True), ("", False), ("   ", False),
	("has space", False), ("$$X$$", False), ("A" * 65, False)])
def test_mapping_token_validation(token, ok):
	"""A mapping token is accepted when it's a plain word (HOSTNAME, loop_0) and
	refused when empty, blank, holding a space or $$, or longer than 64."""
	assert validation.validate_var_map_inner_token(token)[0] is ok


def test_mapping_index_only_for_list_properties():
	"""An index is accepted for a list property (system or user-defined) and no
	index for a plain one; an index on a plain property or a negative index is
	refused."""
	lists = {"vrfs", "uplinks"}  # system list + a user-defined list
	assert validation.validate_var_index(1, "vrfs", lists) == (True, None)
	assert validation.validate_var_index(0, "uplinks", lists) == (True, None)
	assert validation.validate_var_index(None, "hostname", lists) == (True, None)
	assert validation.validate_var_index(0, "hostname", lists)[0] is False
	assert validation.validate_var_index(-1, "vrfs", lists)[0] is False


def test_property_name_checked_against_the_users_definitions():
	"""A property name is accepted only when it's among the user's allowed ones
	(a user-defined one included)."""
	allowed = {"hostname", "rack"}  # includes a user-defined property
	assert validation.validate_var_map_property_name("rack", allowed)[0] is True
	assert validation.validate_var_map_property_name("nope", allowed)[0] is False


# ── Admin restart relaunch ───────────────────────────────────────────────────

@pytest.mark.parametrize("orig_argv,expected_tail", [
	(["python", "-m", "src.webapp"], ["-m", "src.webapp"]),   # module mode
	(["python", "run.py", "--x"], ["run.py", "--x"]),          # script mode
])
def test_restart_relaunches_the_original_invocation(monkeypatch, orig_argv,
                                                    expected_tail):
	"""The restart command is this Python with the original arguments
	(sys.orig_argv), in module mode (-m src.webapp) and script mode alike.

	Under -m, sys.argv[0] is the __main__.py path - relaunching that ran it as
	a script, where `src` isn't importable."""
	monkeypatch.setattr(sys, "argv", [r"C:\repo\src\webapp\__main__.py"])
	monkeypatch.setattr(sys, "orig_argv", orig_argv)
	assert relaunch_command() == [sys.executable, *expected_tail]
