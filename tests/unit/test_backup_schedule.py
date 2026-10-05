"""When a scheduled backup is due (System Settings → Backups), and the
choice settings it uses."""
from datetime import datetime, timedelta

import pytest

from src.db.settings import SETTINGS
from src.webapp.backup_schedule import RETRY_SECONDS, due, last_slot, next_slot

# Wednesday
NOW = datetime(2026, 10, 7, 10, 30)


@pytest.mark.parametrize("schedule, at, weekday, expected", [
	("daily", "02:00", "sun", datetime(2026, 10, 7, 2, 0)),     # earlier today
	("daily", "23:15", "sun", datetime(2026, 10, 6, 23, 15)),   # not yet: yesterday's
	("daily", "10:30", "sun", datetime(2026, 10, 7, 10, 30)),   # this minute counts
	("weekly", "02:00", "wed", datetime(2026, 10, 7, 2, 0)),    # today's, passed
	("weekly", "11:00", "wed", datetime(2026, 9, 30, 11, 0)),   # today's not yet
	("weekly", "02:00", "sun", datetime(2026, 10, 4, 2, 0)),    # last Sunday
	("weekly", "02:00", "thu", datetime(2026, 10, 1, 2, 0)),    # last Thursday
	("off", "02:00", "sun", None),
])
def test_the_latest_scheduled_time(schedule, at, weekday, expected):
	assert last_slot(NOW, schedule, at, weekday) == expected


def test_the_next_scheduled_time():
	assert next_slot(NOW, "daily", "02:00", "sun") == datetime(2026, 10, 8, 2, 0)
	assert next_slot(NOW, "weekly", "02:00", "sun") == datetime(2026, 10, 11, 2, 0)
	assert next_slot(NOW, "off", "02:00", "sun") is None


SLOT = datetime(2026, 10, 7, 2, 0)


def test_due_once_per_scheduled_time_and_caught_up_after_downtime():
	assert due(NOW, SLOT, newest=None, status=None)                    # never backed up
	assert due(NOW, SLOT, newest=SLOT - timedelta(days=1), status=None)  # missed: caught up
	assert not due(NOW, SLOT, newest=SLOT + timedelta(seconds=5), status=None)
	assert not due(NOW, None, newest=None, status=None)                # off


def test_a_failure_is_retried_after_an_hour_not_every_check():
	failed = {"ok": False, "time": (NOW - timedelta(minutes=10)).isoformat()}
	assert not due(NOW, SLOT, newest=None, status=failed)
	failed["time"] = (NOW - timedelta(seconds=RETRY_SECONDS + 1)).isoformat()
	assert due(NOW, SLOT, newest=None, status=failed)
	# a failure before this scheduled time doesn't hold it back
	failed["time"] = (SLOT - timedelta(minutes=1)).isoformat()
	assert due(SLOT + timedelta(minutes=1), SLOT, newest=None, status=failed)


def test_a_choice_setting_accepts_only_its_choices():
	schedule = SETTINGS["backup_schedule"]
	assert schedule.parse(" weekly ") == "weekly"
	with pytest.raises(ValueError, match="must be one of: Off, Daily, Weekly"):
		schedule.parse("hourly")
	# a stored value that isn't a choice (any more) reads as the default
	assert schedule.coerce("hourly") == ("daily", "'hourly' must be one of: Off, Daily, Weekly")


@pytest.mark.parametrize("value, good", [("02:00", True), ("23:59", True), ("0:00", False),
                                         ("24:00", False), ("02:60", False), ("2am", False)])
def test_the_backup_time_is_24_hour_hh_mm(value, good):
	if good:
		assert SETTINGS["backup_time"].parse(value) == value
	else:
		with pytest.raises(ValueError, match="like 02:00"):
			SETTINGS["backup_time"].parse(value)
