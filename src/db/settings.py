"""System settings: the registry of what settings exist, and the store.

The registry (SETTINGS, RULES) is code: each setting's type, default, range,
page text and when a change applies. The database (system_settings) holds
only values an admin changed — no row means "not changed". So a default
changed in a new release reaches every install that never touched it, and
"reset to default" is deleting the row.

Effective value: admin's change (DB) > install-time value (env, if the
setting has one) > code default.

Settings read by SQL (the pg_cron retention jobs run inside Postgres) can't
see the app's environment, so they never have an install-time env var —
enforced below.
"""
import os
import uuid
from dataclasses import dataclass
from typing import Callable

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
	env: str | None = None      # install-time value (install.py / compose)
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
		value = "" if raw is None else str(raw).strip()
		return value


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

# Rules that involve more than one setting: (keys, check(values), message)
RULES: list[tuple[tuple[str, ...], Callable[[dict], bool], str]] = [
	(("log_retention_days", "job_retention_days"),
	 lambda v: v["log_retention_days"] >= v["job_retention_days"],
	 "Log files must be kept at least as long as job records — Download "
	 "Log needs the file while the job exists."),
	(("config_snapshot_retention_days", "job_retention_days"),
	 lambda v: v["config_snapshot_retention_days"] <= v["job_retention_days"],
	 "Config snapshots can't be kept longer than their job record."),
]

for _s in SETTINGS.values():
	assert not (_s.sql and _s.env), (
		f"{_s.key}: read by SQL, so it can't have an install-time env var")


def sql_value(key: str) -> str:
	"""SQL expression for an int setting's current value, for statements that
	run inside Postgres: the admin's value, else the code default."""
	s = SETTINGS[key]
	assert s.sql and s.kind is int
	return (f"COALESCE((SELECT (value #>> '{{}}')::int FROM system_settings "
	        f"WHERE key = '{key}'), {int(s.default)})")


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
	"""Read and change settings. `postgres` returns the live connection —
	looked up per call, so a Server Management database switch is followed."""

	def __init__(self, postgres: Callable):
		self._postgres = postgres

	# ── reading ──
	def _rows(self) -> dict[str, object]:
		with self._postgres().get_session() as db_session:
			return {r.key: r.value for r in db_session.query(SystemSetting)}

	@staticmethod
	def _env_value(s: Setting):
		raw = os.environ.get(s.env) if s.env else None
		if raw in (None, ""):
			return None
		try:
			return s.parse(raw)
		except ValueError:
			return None   # a bad install value falls back to the default

	def _resolve(self, s: Setting, rows: dict) -> tuple[object, str]:
		if s.key in rows:
			try:
				return s.parse(rows[s.key]), "admin"
			except ValueError:
				pass   # out of range after a registry change: ignore the row
		env_value = self._env_value(s)
		if env_value is not None:
			return env_value, "install"
		return s.default, "default"

	def get(self, key: str):
		s = SETTINGS[key]
		return self._resolve(s, self._rows())[0]

	def values(self) -> dict[str, object]:
		rows = self._rows()
		return {k: self._resolve(s, rows)[0] for k, s in SETTINGS.items()}

	def list_for_display(self) -> list[dict]:
		"""Every setting with its effective value and where it came from, in
		registry order (the page groups by card)."""
		rows = self._rows()
		out = []
		for s in SETTINGS.values():
			value, source = self._resolve(s, rows)
			out.append({
				"key": s.key, "label": s.label, "help": s.help, "card": s.card,
				"value": value, "source": source, "default": s.default,
				"install_value": self._env_value(s), "env": s.env,
				"minimum": s.minimum, "maximum": s.maximum,
				"applies": s.applies, "editable": s.editable,
				"kind": s.kind.__name__,
			})
		return out

	# ── changing ──
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
		merged = {**current, **parsed}
		for keys, check, message in RULES:
			if any(k in parsed for k in keys) and not check(merged):
				errors[None] = message
		if errors:
			raise SettingsError(errors)
		changes = [Change(k, current[k], v) for k, v in parsed.items()
		           if v != current[k]]
		if changes:
			with self._postgres().get_session() as db_session:
				for c in changes:
					row = db_session.get(SystemSetting, c.key)
					if row is None:
						db_session.add(SystemSetting(key=c.key, value=c.new,
						                             updated_by=user_id))
					else:
						row.value, row.updated_by = c.new, user_id
		return changes

	def reset(self, key: str) -> Change | None:
		"""Back to the install value / default: delete the admin's row.
		Refused (SettingsError) if the result would break a rule."""
		s = SETTINGS[key]
		rows = self._rows()
		if key not in rows:
			return None
		current = {k: self._resolve(x, rows)[0] for k, x in SETTINGS.items()}
		without = {k: v for k, v in rows.items() if k != key}
		after = self._resolve(s, without)[0]
		merged = {**current, key: after}
		for keys, check, message in RULES:
			if key in keys and not check(merged):
				raise SettingsError({None: message})
		with self._postgres().get_session() as db_session:
			row = db_session.get(SystemSetting, key)
			if row is not None:
				db_session.delete(row)
		return Change(key, current[key], after)
