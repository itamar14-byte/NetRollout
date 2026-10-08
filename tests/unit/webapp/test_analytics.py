"""The analytics query builder (compile_query_rules): the QueryBuilder rules
 -> SQL, only the allowed fields and operators. No app, no DB."""
import warnings

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SADeprecationWarning

from src.webapp.blueprints.analytics import (QUERY_AUDIT_LOG_FIELDS, QUERY_DEVICE_RESULT_FIELDS,
                                             compile_query_rules)


def sql(expr) -> str:
	"""The expression as PostgreSQL SQL text, values inlined."""
	return str(expr.compile(dialect=postgresql.dialect(),
	                        compile_kwargs={"literal_binds": True}))


def leaf(field, operator, value):
	return {"field": field, "operator": operator, "value": value}


# ── compile_query_rules: the QueryBuilder -> SQL allowlist ───────────────────

class TestCompileQueryRules:

	@pytest.mark.parametrize("condition, expected", [("AND", "true"), ("OR", "false")])
	def test_an_empty_group_needs_no_deprecated_call(self, condition, expected):
		"""Every rule deleted: an empty AND group matches every row, an empty OR group
		none - without SQLAlchemy's empty and_() / or_(), deprecated (a later release
		drops it, which would turn the approved "all rows" into an error)."""
		with warnings.catch_warnings():
			warnings.simplefilter("error", SADeprecationWarning)
			expr = compile_query_rules({"condition": condition, "rules": []},
			                           QUERY_DEVICE_RESULT_FIELDS)
		assert sql(expr) == expected

	def test_a_group_with_rules_compiles_as_before(self):
		"""The always-true start of an AND group leaves no trace in the SQL."""
		expr = compile_query_rules({"condition": "AND", "rules": [
			leaf("status", "equal", "failed"), leaf("status", "equal", "partial")]},
			QUERY_DEVICE_RESULT_FIELDS)
		assert sql(expr) == ("device_results.status = 'failed' AND "
		                     "device_results.status = 'partial'")

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
