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
           config=None, age_days=0, transcript=None, diff=None):
	"""A stand-in device result row: its own job unless one is given,
	completed age_days ago; transcript and diff when it was verified."""
	return SimpleNamespace(
		status=status, device_ip=ip, job_id=job_id or uuid.uuid4(),
		commands_sent=sent, commands_verified=verified, fetched_config=config,
		transcript=transcript, config_diff=diff,
		completed_at=dt.datetime.now() - dt.timedelta(days=age_days))


JUDGED = {"commands": [], "other_changes": []}


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


@pytest.mark.parametrize("statuses,expected", [
	(["success", "review"], "review"),
	(["review", "not verified"], "review"),
	(["success", "not verified"], "not verified"),
	(["not verified", "not verified"], "not verified"),
	(["review", "failed"], "partial"),
	(["review", "cancelled"], "cancelled"),
])
def test_job_status_with_review_and_not_verified(statuses, expected):
	"""A device in review makes the job review; one not verified makes it not
	verified (review first) - unless something failed (partial) or was
	cancelled."""
	assert job_status([result(s) for s in statuses]) == expected


def test_job_status_condition_refuses_an_unknown_status():
	"""job_status_condition has a condition for every status job_status
	gives, and refuses any other (no silent "match nothing")."""
	for status in JOB_STATUSES:
		assert job_status_condition(status) is not None
	with pytest.raises(ValueError):
		job_status_condition("bogus")


class TestConfigExpired:
	"""A verified device keeps its configuration diff (and, in review, the
	configuration after) until the snapshot retention clears it."""
	DAYS = SETTINGS["config_snapshot_retention_days"].default
	OLD = DAYS + 1

	def test_a_verified_device_past_the_window_without_its_diff_is_expired(self):
		"""Judged (a transcript), older than the snapshot retention, its diff
		cleared: reported as expired."""
		assert config_expired(result("review", verified=1, age_days=self.OLD,
		                             transcript=JUDGED), self.DAYS)

	def test_within_window_is_not_expired(self):
		"""Inside the retention window it isn't expired."""
		assert not config_expired(result("review", verified=1, age_days=1,
		                                 transcript=JUDGED), self.DAYS)

	def test_window_follows_the_setting(self):
		"""The same 3-day-old row is not expired with a 7-day retention and is
		expired with a 2-day one."""
		row = result("review", verified=1, age_days=3, transcript=JUDGED)
		assert not config_expired(row, 7)
		assert config_expired(row, 2)

	def test_diff_still_present_is_not_expired(self):
		"""An old verified device whose diff is still stored isn't expired."""
		assert not config_expired(
			result("success", verified=2, age_days=self.OLD, transcript=JUDGED, diff="+x"),
			self.DAYS)

	def test_a_device_never_judged_never_had_a_snapshot(self):
		"""Not verified (or a job from before verification by configuration):
		no transcript, nothing kept - not expired."""
		assert not config_expired(
			result("not verified", verified=None, age_days=self.OLD), self.DAYS)
