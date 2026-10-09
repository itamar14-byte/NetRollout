"""When a scheduled backup is due (System Settings → Backups), and the
choice settings it uses."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.backup import schedule as backup_schedule
from src.backup.schedule import RETRY_SECONDS, due, last_slot, next_slot
from src.db.settings import SETTINGS


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
	"""The latest scheduled time at or before now, daily or weekly (today's when passed,
	this minute included, else the previous one); none when off."""
	assert last_slot(NOW, schedule, at, weekday) == expected


def test_the_next_scheduled_time():
	"""The next scheduled time, daily or weekly; none when off."""
	assert next_slot(NOW, "daily", "02:00", "sun") == datetime(2026, 10, 8, 2, 0)
	assert next_slot(NOW, "weekly", "02:00", "sun") == datetime(2026, 10, 11, 2, 0)
	assert next_slot(NOW, "off", "02:00", "sun") is None


SLOT = datetime(2026, 10, 7, 2, 0)


def test_due_once_per_scheduled_time_and_caught_up_after_downtime():
	"""A backup is due when none was made since the scheduled time (never, or a missed
	one caught up); not after one made since, nor when off."""
	assert due(NOW, SLOT, newest=None, status=None)                    # never backed up
	assert due(NOW, SLOT, newest=SLOT - timedelta(days=1), status=None)  # missed: caught up
	assert not due(NOW, SLOT, newest=SLOT + timedelta(seconds=5), status=None)
	assert not due(NOW, None, newest=None, status=None)                # off


def test_a_failure_is_retried_after_an_hour_not_every_check():
	"""After a failure the backup is due again only once the retry time has passed; a
	failure before this scheduled time doesn't hold it back."""
	failed = {"ok": False, "time": (NOW - timedelta(minutes=10)).isoformat()}
	assert not due(NOW, SLOT, newest=None, status=failed)
	failed["time"] = (NOW - timedelta(seconds=RETRY_SECONDS + 1)).isoformat()
	assert due(NOW, SLOT, newest=None, status=failed)
	# a failure before this scheduled time doesn't hold it back
	failed["time"] = (SLOT - timedelta(minutes=1)).isoformat()
	assert due(SLOT + timedelta(minutes=1), SLOT, newest=None, status=failed)


def test_a_choice_setting_accepts_only_its_choices():
	"""A choice setting accepts its choices (trimmed) and refuses others; a stored value
	that isn't a choice reads as the default, with the reason."""
	schedule = SETTINGS["backup_schedule"]
	assert schedule.parse(" weekly ") == "weekly"
	with pytest.raises(ValueError, match="must be one of: Off, Daily, Weekly"):
		schedule.parse("hourly")
	# a stored value that isn't a choice (any more) reads as the default
	assert schedule.coerce("hourly") == ("daily", "'hourly' must be one of: Off, Daily, Weekly")


@pytest.mark.parametrize("value, good", [("02:00", True), ("23:59", True), ("0:00", False),
                                         ("24:00", False), ("02:60", False), ("2am", False)])
def test_the_backup_time_is_24_hour_hh_mm(value, good):
	"""The backup time accepts only 24-hour HH:MM (00:00-23:59, two-digit hour)."""
	if good:
		assert SETTINGS["backup_time"].parse(value) == value
	else:
		with pytest.raises(ValueError, match="like 02:00"):
			SETTINGS["backup_time"].parse(value)


# ── The scheduler thread (start_backup_schedule), driven turn by turn ────────

class _Stop(Exception):
	"""Ends the loop: raised by the fake sleep."""


def test_the_scheduler_waits_first_then_ticks_unless_held_and_survives_failures(
		monkeypatch, capsys):
	"""The thread (daemon, "backup-schedule") sleeps FIRST_CHECK_SECONDS before its
	first check, then each turn calls tick(backend) unless hold() is True, prints
	"backup schedule check failed: ..." when tick raises and keeps going, and
	sleeps CHECK_SECONDS after every turn."""
	thread, events = {}, []
	backend = SimpleNamespace(name="the-backend")
	holds = iter([False, True, False, False])

	class FakeThread:
		def __init__(self, **kwargs):
			thread.update(kwargs)

		def start(self):
			thread["started"] = True

	def sleep(seconds):
		events.append(("sleep", seconds))
		if len([e for e in events if e[0] == "sleep"]) == 5:
			raise _Stop

	def tick(b):
		events.append(("tick", b))
		if len([e for e in events if e[0] == "tick"]) == 2:
			raise OSError("the backups folder is full")

	def hold():
		events.append(("hold",))
		return next(holds)

	monkeypatch.setattr(backup_schedule.threading, "Thread", FakeThread)
	monkeypatch.setattr(backup_schedule, "time", SimpleNamespace(sleep=sleep))
	monkeypatch.setattr(backup_schedule, "tick", tick)
	backup_schedule.start_backup_schedule(backend, hold)
	assert thread["started"] and thread["daemon"] is True
	assert thread["name"] == "backup-schedule"
	with pytest.raises(_Stop):
		thread["target"]()
	first, check = backup_schedule.FIRST_CHECK_SECONDS, backup_schedule.CHECK_SECONDS
	assert events == [("sleep", first),
	                  ("hold",), ("tick", backend), ("sleep", check),
	                  ("hold",), ("sleep", check),                      # held
	                  ("hold",), ("tick", backend), ("sleep", check),   # fails
	                  ("hold",), ("tick", backend), ("sleep", check)]   # goes on
	assert capsys.readouterr().out == \
		"[NetRollout] backup schedule check failed: the backups folder is full\n"


def test_the_scheduler_survives_a_failing_hold(monkeypatch, capsys):
	"""hold() raising is caught like a failing tick: printed, no tick, and the
	loop sleeps CHECK_SECONDS and goes on."""
	thread, sleeps, ticks = {}, [], []

	class FakeThread:
		def __init__(self, **kwargs):
			thread.update(kwargs)

		def start(self):
			pass

	def sleep(seconds):
		sleeps.append(seconds)
		if len(sleeps) == 3:
			raise _Stop

	def hold():
		raise RuntimeError("no move state")

	monkeypatch.setattr(backup_schedule.threading, "Thread", FakeThread)
	monkeypatch.setattr(backup_schedule, "time", SimpleNamespace(sleep=sleep))
	monkeypatch.setattr(backup_schedule, "tick", ticks.append)
	backup_schedule.start_backup_schedule(SimpleNamespace(), hold)
	with pytest.raises(_Stop):
		thread["target"]()
	assert sleeps == [backup_schedule.FIRST_CHECK_SECONDS,
	                  backup_schedule.CHECK_SECONDS, backup_schedule.CHECK_SECONDS]
	assert ticks == []
	assert capsys.readouterr().out == (
		"[NetRollout] backup schedule check failed: no move state\n" * 2)
