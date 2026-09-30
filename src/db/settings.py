"""System settings: the registry (what settings exist and their rules) and
the store (reading and changing them).

The database table is the only runtime source: every setting has a row.
Rows are seeded at every startup (seed_settings, from install()): a missing
setting gets its install-time value (env, if the setting has one) or its
default; existing rows are never touched. So a fresh install is fully
populated, an upgrade gains only new settings, and an install keeps its
values across upgrades until an admin changes them.

The registry here defines each setting — type, range, page text, when a
change applies, its seed value — and the cross-setting rules. The rules are
declarative (left >= / <= right) so the System Settings page can enforce
exactly the same checks in the browser (rules_for_client) that update()
enforces on the server.
"""
import operator
import os
import uuid
from dataclasses import dataclass
from typing import Callable

from sqlalchemy.orm import Session

from src.db.tables import SystemSetting
from src.logging_utils import LOG_RETENTION_DAYS


@dataclass(frozen=True)
class Setting:
	key: str
	label: str
	help: str
	card: str
	default: int | str | None
	kind: type = int
	minimum: int | None = None
	maximum: int | None = None
	applies: str = "immediately"
	env: str | None = None      # install-time value: seeds a new row only
	sql: bool = False           # also read inside Postgres (pg_cron)
	editable: bool = True

	def parse(self, raw):
		"""Raw input (form string, JSON value, env string) → typed value.
		Raises ValueError with a message for the page."""
		if self.kind is int:
			if isinstance(raw, bool):
				raise ValueError("must be a whole number")
			try:
				value = int(str(raw).strip())
			except (TypeError, ValueError):
				raise ValueError("must be a whole number") from None
			if self.minimum is not None and value < self.minimum:
				raise ValueError(f"must be at least {self.minimum}")
			if self.maximum is not None and value > self.maximum:
				raise ValueError(f"must be at most {self.maximum}")
			return value
		return "" if raw is None else str(raw).strip()

	def coerce(self, stored) -> tuple[object, str | None]:
		"""A stored value made usable: (value, problem). Out of range → the
		nearest valid value (e.g. a range tightened in a release); unreadable
		→ the default. Never raises — reading a setting must not crash."""
		try:
			return self.parse(stored), None
		except ValueError as e:
			if self.kind is int:
				try:
					n = int(str(stored).strip())
					lo = self.minimum if self.minimum is not None else n
					hi = self.maximum if self.maximum is not None else n
					return min(max(n, lo), hi), f"{stored!r} {e}"
				except (TypeError, ValueError):
					pass
			return self.default, f"{stored!r} {e}"

	def seed_value(self):
		"""Value for a new row: the install-time env value if valid, else the
		default."""
		raw = os.environ.get(self.env) if self.env else None
		if raw not in (None, ""):
			try:
				return self.parse(raw)
			except ValueError:
				pass
		return self.default


SETTINGS: dict[str, Setting] = {s.key: s for s in [
	# ── Retention ──
	Setting("job_retention_days", "Job records",
	        "A rollout's results and commands (Results, Download Log).",
	        "Retention", 30, minimum=1, maximum=3650,
	        applies="next nightly clean-up", sql=True),
	Setting("config_snapshot_retention_days", "Config snapshots",
	        "Running configs fetched by Verify (Verify Diff). The job record "
	        "itself is kept longer.",
	        "Retention", 7, minimum=1, maximum=3650,
	        applies="next nightly clean-up", sql=True),
	Setting("audit_retention_days", "Audit log",
	        "Who did what, when (Admin → Audit).",
	        "Retention", 90, minimum=7, maximum=3650,
	        applies="next nightly clean-up", sql=True),
	Setting("log_retention_days", "Log files",
	        "Files in the logs/ folder — browsable on the server for "
	        "troubleshooting after the job record is gone.",
	        "Retention", LOG_RETENTION_DAYS, minimum=1, maximum=3650,
	        applies="next daily log clean-up"),
]}


@dataclass(frozen=True)
class Rule:
	"""Declarative cross-setting rule: SETTINGS[left] <op> SETTINGS[right]."""
	left: str
	op: str       # ">=" or "<="
	right: str
	message: str

	def holds(self, values: dict) -> bool:
		return _OPS[self.op](values[self.left], values[self.right])


_OPS = {">=": operator.ge, "<=": operator.le}

RULES: list[Rule] = [
	Rule("log_retention_days", ">=", "job_retention_days",
	     "Log files must be kept at least as long as job records — Download "
	     "Log needs the file while the job exists."),
	Rule("config_snapshot_retention_days", "<=", "job_retention_days",
	     "Config snapshots can't be kept longer than their job record."),
]

for _s in SETTINGS.values():
	assert not (_s.sql and _s.env), (
		f"{_s.key}: read by SQL, so it can't have an install-time env var")
for _r in RULES:
	assert _r.op in _OPS and _r.left in SETTINGS and _r.right in SETTINGS


def rules_for_client() -> list[dict]:
	"""The rules as data, for the page's browser-side checks."""
	return [{"left": r.left, "op": r.op, "right": r.right,
	         "message": r.message} for r in RULES]


def sql_value(key: str) -> str:
	"""SQL expression for an int setting, for statements that run inside
	Postgres. The row always exists after seeding; the COALESCE is only a
	safety net if seeding failed."""
	s = SETTINGS[key]
	assert s.sql and s.kind is int
	return (f"COALESCE((SELECT (value #>> '{{}}')::int FROM system_settings "
	        f"WHERE key = '{key}'), {int(s.default)})")


def seed_settings(db_session: Session) -> list[str]:
	"""Insert every missing setting (install value or default); never touch
	existing rows. Returns problems found in existing rows, for the log."""
	existing = {r.key: r.value for r in db_session.query(SystemSetting)}
	for key, s in SETTINGS.items():
		if key not in existing:
			db_session.add(SystemSetting(key=key, value=s.seed_value()))
	problems = []
	for key, stored in existing.items():
		if key in SETTINGS:
			value, problem = SETTINGS[key].coerce(stored)
			if problem:
				problems.append(f"setting {key}: stored {problem} — using {value!r}")
	return problems


class SettingsError(ValueError):
	"""Validation failed: {key: message} (key None for cross-setting rules)."""

	def __init__(self, errors: dict[str | None, str]):
		super().__init__("; ".join(errors.values()))
		self.errors = errors


@dataclass(frozen=True)
class Change:
	key: str
	old: object
	new: object


class SettingsStore:
	"""Read and change settings — always the table. `postgres` returns the
	live connection, looked up per call, so a Server Management database
	switch is followed."""

	def __init__(self, postgres: Callable):
		self._postgres = postgres

	# ── reading ──
	def _rows(self) -> dict[str, SystemSetting]:
		with self._postgres().get_session() as db_session:
			rows = {r.key: r for r in db_session.query(SystemSetting)}
			db_session.expunge_all()
			return rows

	@staticmethod
	def _value(s: Setting, rows: dict) -> object:
		row = rows.get(s.key)
		# a missing row only happens if seeding failed: use the default
		return s.coerce(row.value)[0] if row is not None else s.default

	def get(self, key: str):
		return self._value(SETTINGS[key], self._rows())

	def values(self) -> dict[str, object]:
		rows = self._rows()
		return {k: self._value(s, rows) for k, s in SETTINGS.items()}

	def list_for_display(self) -> list[dict]:
		"""Every setting in registry order, with what the page shows: value,
		default, whether it differs from the default and whether that came
		from the install (seeded from env, never changed by an admin)."""
		rows = self._rows()
		out = []
		for s in SETTINGS.values():
			row = rows.get(s.key)
			value = self._value(s, rows)
			changed = value != s.default
			out.append({
				"key": s.key, "label": s.label, "help": s.help, "card": s.card,
				"value": value, "default": s.default, "changed": changed,
				"from_install": changed and row is not None
				                and row.updated_by is None,
				"updated_at": row.updated_at if row is not None else None,
				"minimum": s.minimum, "maximum": s.maximum,
				"applies": s.applies, "editable": s.editable,
				"kind": s.kind.__name__,
			})
		return out

	# ── changing ──
	def _check_rules(self, merged: dict, touched: set[str]) -> dict:
		return {None: r.message for r in RULES
		        if (r.left in touched or r.right in touched)
		        and not r.holds(merged)}

	def _write(self, values: dict[str, object], user_id) -> None:
		with self._postgres().get_session() as db_session:
			for key, value in values.items():
				row = db_session.get(SystemSetting, key)
				if row is None:
					db_session.add(SystemSetting(key=key, value=value,
					                             updated_by=user_id))
				else:
					row.value, row.updated_by = value, user_id

	def update(self, raw: dict[str, object], user_id: uuid.UUID | None) -> list[Change]:
		"""Validate and save several settings at once; returns what changed.
		All-or-nothing: raises SettingsError without saving on any problem."""
		errors, parsed = {}, {}
		for key, value in raw.items():
			s = SETTINGS.get(key)
			if s is None or not s.editable:
				errors[key] = "not an editable setting"
				continue
			try:
				parsed[key] = s.parse(value)
			except ValueError as e:
				errors[key] = f"{s.label} {e}"
		if errors:
			raise SettingsError(errors)
		current = self.values()
		errors = self._check_rules({**current, **parsed}, set(parsed))
		if errors:
			raise SettingsError(errors)
		changes = [Change(k, current[k], v) for k, v in parsed.items()
		           if v != current[k]]
		if changes:
			self._write({c.key: c.new for c in changes}, user_id)
		return changes

	def reset(self, key: str, user_id: uuid.UUID | None) -> Change | None:
		"""Back to the default (written into the row, like any change).
		Refused (SettingsError) if the result would break a rule."""
		s = SETTINGS[key]
		current = self.values()
		if current[key] == s.default:
			return None
		errors = self._check_rules({**current, key: s.default}, {key})
		if errors:
			raise SettingsError(errors)
		self._write({key: s.default}, user_id)
		return Change(key, current[key], s.default)
