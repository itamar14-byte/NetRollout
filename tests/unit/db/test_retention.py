"""The nightly clean-up's schedule (src/db/retention.py): 03:00, once a
day, a missed time caught up once, a restart not running it again."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from src import runtime
from src.db import retention
from src.db.retention import due


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


# ── The daemon loop (start_retention), driven turn by turn ───────────────────

class _Stop(Exception):
	"""Ends the loop: raised by the fake sleep."""


def _drive(monkeypatch, times, hold=lambda: False, run_once=None, turns=None):
	"""Start the retention loop with a fake thread, clock and sleep; run its
	target until the sleep has been called `turns` times (default: one per
	time in `times`, then one more that stops it).

	:returns: (the thread's kwargs, the sleeps, the run_once calls)"""
	thread, sleeps, runs = {}, [], []
	clock = iter(times)
	turns = len(times) + 1 if turns is None else turns

	class FakeThread:
		def __init__(self, **kwargs):
			thread.update(kwargs)

		def start(self):
			thread["started"] = True

	def sleep(seconds):
		sleeps.append(seconds)
		if len(sleeps) >= turns:
			raise _Stop

	class FakeDateTime(datetime):
		@classmethod
		def now(cls, tz=None):
			return next(clock)

	def fake_run_once(engine, now):
		runs.append((engine, now))
		if run_once:
			run_once(now)

	monkeypatch.setattr(runtime.threading, "Thread", FakeThread)
	monkeypatch.setattr(runtime, "time", SimpleNamespace(sleep=sleep))
	monkeypatch.setattr(retention, "datetime", FakeDateTime)
	monkeypatch.setattr(retention, "run_once", fake_run_once)
	backend = SimpleNamespace(postgres=SimpleNamespace(engine="the-engine"))
	retention.start_retention(backend, hold)
	assert thread["started"] and thread["daemon"] is True and thread["name"] == "retention"
	with pytest.raises(_Stop):
		thread["target"]()
	return thread, sleeps, runs


def test_the_loop_runs_once_a_day_waits_for_a_hold_and_retries_after_an_hour(
		monkeypatch, capsys):
	"""Each turn sleeps CHECK_SECONDS, then: not due -> skipped; due while hold() is
	True -> skipped; due -> run_once(engine, now); a failure prints the ACTION
	NEEDED line and nothing runs until an hour later (RETRY) even though due; the
	retry's success makes the day done (not run again that day). hold() is asked
	only when it's due and not waiting for a retry."""
	(runtime.config_dir() / retention.STATUS_FILE).unlink(missing_ok=True)   # never ran
	day = lambda h, m: datetime(2026, 10, 6, h, m)
	times = [day(2, 0),       # not due yet
	         day(3, 5),       # due, but held
	         day(3, 6),       # due: fails
	         day(3, 30),      # waiting for the retry
	         day(4, 6),       # the retry time: runs, succeeds
	         day(5, 0)]       # done today
	holds = iter([True, False, False])
	asked = []

	def hold():
		asked.append(True)
		return next(holds)

	def run_once(now):
		if now == day(3, 6):
			raise RuntimeError("the database is down")

	_, sleeps, runs = _drive(monkeypatch, times, hold, run_once)
	assert sleeps == [retention.CHECK_SECONDS] * 7
	assert runs == [("the-engine", day(3, 6)), ("the-engine", day(4, 6))]
	assert len(asked) == 3
	assert capsys.readouterr().out == (
		"[NetRollout] ACTION NEEDED - the nightly clean-up failed: the database is "
		"down (tried again in an hour)\n")


def test_the_loop_starts_from_the_last_recorded_success(monkeypatch):
	"""A success recorded today (the status file) means the loop doesn't run it again
	that day after a restart; the next day it runs."""
	_status({"time": "2026-10-06T03:00:41", "ok": True, "counts": {}})
	times = [datetime(2026, 10, 6, 10, 0), datetime(2026, 10, 7, 3, 0)]
	_, _, runs = _drive(monkeypatch, times)
	assert runs == [("the-engine", datetime(2026, 10, 7, 3, 0))]


def test_the_loop_sleeps_before_its_first_check(monkeypatch):
	"""The first thing the loop does is sleep CHECK_SECONDS - nothing is looked at
	before (the clock isn't read)."""
	_, sleeps, runs = _drive(monkeypatch, [], turns=1)
	assert sleeps == [retention.CHECK_SECONDS] and runs == []
