"""The nightly clean-up's schedule (src/webapp/retention.py): 03:00, once a
day, a missed time caught up once, a restart not running it again."""
import json
from datetime import datetime

import pytest

from src import runtime
from src.webapp import retention
from src.webapp.retention import due


@pytest.mark.parametrize("now, last_run, expected", [
	(datetime(2026, 10, 6, 2, 59), None, False),                       # not yet
	(datetime(2026, 10, 6, 3, 0), None, True),                         # at 03:00
	(datetime(2026, 10, 6, 10, 0), None, True),                        # started later: caught up
	(datetime(2026, 10, 6, 10, 0), datetime(2026, 10, 6, 3, 1), False),  # done today
	(datetime(2026, 10, 6, 10, 0), datetime(2026, 10, 5, 3, 1), True),   # yesterday's
	(datetime(2026, 10, 7, 2, 0), datetime(2026, 10, 6, 3, 1), False),   # tomorrow, before 03:00
])
def test_once_a_day_at_three(now, last_run, expected):
	"""The clean-up is due from 03:00 once a day: not before, caught up later that day,
	not again after today's run, due again if the last run was yesterday's."""
	assert due(now, last_run) is expected


def _status(payload):
	"""Writes the clean-up's status file with `payload`."""
	runtime.config_dir().mkdir(parents=True, exist_ok=True)
	(runtime.config_dir() / retention.STATUS_FILE).write_text(json.dumps(payload), encoding="utf-8")


def test_a_restart_remembers_todays_run():
	"""A successful run recorded in the status file counts as today's after a restart,
	so the clean-up isn't due again."""
	_status({"time": "2026-10-06T03:00:41", "ok": True, "counts": {}})
	assert retention._last_success() == datetime(2026, 10, 6, 3, 0, 41)
	assert not due(datetime(2026, 10, 6, 9, 0), retention._last_success())


def test_a_failed_run_isnt_a_run():
	"""A failed run in the status file is no last success."""
	_status({"time": "2026-10-06T03:00:41", "ok": False, "message": "the database is down"})
	assert retention._last_success() is None


def test_nothing_recorded_yet():
	"""With no status file there is no status and no last success."""
	(runtime.config_dir() / retention.STATUS_FILE).unlink(missing_ok=True)
	assert retention.read_status() is None and retention._last_success() is None
