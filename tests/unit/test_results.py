"""Finished jobs' history (src/results.py): who may see a job, whose
numbers a page shows (the admin's ?user= choice), the KPIs, a job's status
and a config snapshot's expiry."""
import datetime as dt
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.accounts.users import Viewer
from src.db.settings import SETTINGS
from src.results import (JOB_STATUSES, JobResults, build_kpi, config_expired, job_status,
                         job_status_condition)

ME, OTHER = uuid.uuid4(), uuid.uuid4()


def history(is_admin=False):
	"""JobResults for ME (the rules need no session)."""
	return JobResults(MagicMock(), Viewer(ME, is_admin))


def test_a_job_is_seen_by_its_owner_and_any_admin():
	"""may_see: the owner sees their job, another operator doesn't, an admin
	sees anyone's."""
	assert history().may_see(ME)
	assert not history().may_see(OTHER)
	assert history(is_admin=True).may_see(OTHER)


@pytest.mark.parametrize("raw", [None, "me", " me ", "not-a-uuid", ""])
def test_an_admins_own_numbers_unless_a_user_is_picked(raw):
	"""scope_user: no choice, "me" or something that isn't an id gives the
	admin's own numbers, shown as "me"."""
	assert history(is_admin=True).scope_user(raw) == (ME, "me")


def test_an_admin_may_pick_any_user():
	"""scope_user: an admin's ?user=<id> (spaces around it ignored) is that
	user's numbers, shown by the id as given."""
	assert history(is_admin=True).scope_user(f" {OTHER} ") == (OTHER, str(OTHER))


def test_an_operator_always_sees_their_own():
	"""scope_user: an operator's ?user= is ignored."""
	assert history().scope_user(str(OTHER)) == (ME, "me")


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
	"""Four results (1 success) for two devices over three jobs: success rate 25 %,
	3 jobs, 8 commands pushed, the device that failed twice named by its label as the
	top failed - and "device_pushes" 4: it counts device results (pushes, failed
	ones included), not distinct devices, so each device counts once per rollout
	(the Analytics tile says "device push operations")."""
	job = uuid.uuid4()
	rows = [result("success", job_id=job), result("failed", "10.0.0.9", job),
	        result("failed", "10.0.0.9"), result("partial")]
	assert len({r.device_ip for r in rows}) == 2
	kpi = build_kpi(rows, {"10.0.0.9": "edge-9"})
	assert kpi["success_rate"] == 25
	assert kpi["jobs_30d"] == 3
	assert kpi["device_pushes"] == len(rows) == 4
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


def test_job_status_condition_refuses_an_unknown_status():
	"""job_status_condition has a condition for every status job_status
	gives, and refuses any other (no silent "match nothing")."""
	for status in JOB_STATUSES:
		assert job_status_condition(status) is not None
	with pytest.raises(ValueError):
		job_status_condition("bogus")


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
