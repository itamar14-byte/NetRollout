"""The shipped Grafana panels count what their titles say, by NetRollout's own
rules: each panel's SQL is read from its dashboard file and run on jobs that
cover every rule of src/results.job_status, then compared with Results'
answer. Grafana's macros are filled in as Grafana would for "everything"
($__timeFilter: no limit; $__timeGroupAlias: one bucket per day)."""
import datetime as dt
import json
import re
import uuid
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import text

from src.db.tables import DeviceResult
from src.results import job_status

DASHBOARDS = Path(__file__).resolve().parents[2] / "deploy" / "grafana" / "dashboards"

# one job per rule of job_status: its devices' statuses
JOBS = {
	"mixed success + failed": ["success", "failed"],
	"all failed": ["failed", "failed"],
	"success + review": ["success", "review"],
	"success + not verified": ["success", "not verified"],
	"all success": ["success", "success"],
	"cancelled + success": ["cancelled", "success"],
	"one partial": ["partial"],
}


def panel_sql(dashboard: str, title: str, job_id: uuid.UUID | None = None) -> str:
	""":returns: the panel's SQL, with Grafana's macros filled in"""
	data = json.loads((DASHBOARDS / dashboard).read_text(encoding="utf-8"))
	(spec,) = [el["spec"] for el in data["spec"]["elements"].values()
	           if el["spec"].get("title") == title]
	(sql,) = re.findall(r'"rawSql": ("(?:[^"\\]|\\.)*")', json.dumps(spec))
	sql = json.loads(sql)
	sql = re.sub(r"\$__timeFilter\((\w+)\)", r"(\1 IS NOT NULL)", sql)
	sql = re.sub(r"\$__timeGroupAlias\((\w+),\s*\$__interval\)", r"date_trunc('day', \1) AS time", sql)
	return sql.replace("$job_id", str(job_id))


@pytest.fixture
def jobs(make_user, session_scope):
	""":returns: {name: job id} - the JOBS, today, each device 10 s after the
	 job's start (a job's duration: its devices' span, (n-1)*10 + 5 s)"""
	user = make_user()
	start = dt.datetime.now().replace(microsecond=0)
	ids = {}
	with session_scope() as s:
		for n, (name, statuses) in enumerate(JOBS.items()):
			ids[name] = job = uuid.uuid4()
			for i, status in enumerate(statuses):
				begun = start + dt.timedelta(minutes=n, seconds=10 * i)
				s.add(DeviceResult(user_id=user.id, job_id=job, started_at=begun,
				                   completed_at=begun + dt.timedelta(seconds=5),
				                   device_ip=f"10.0.{n}.{i + 1}", device_port=22,
				                   device_type="cisco_ios", commands_sent=1, status=status))
	return ids


def statuses_by_results(session_scope, ids):
	""":returns: {job id: its status as Results computes it}"""
	with session_scope() as s:
		return {job: job_status(s.query(DeviceResult).filter_by(job_id=job).all())
		        for job in ids.values()}


def test_job_details_status_is_the_job_s(jobs, session_scope):
	"""Job Details' Job Status is the job's status (Results' rule), not one
	device's: e.g. a success and a failed device make a partial job."""
	expected = statuses_by_results(session_scope, jobs)
	with session_scope() as s:
		shown = {job: s.execute(text(panel_sql("Jobs/job_details.json", "Job Status", job))).scalar()
		         for job in jobs.values()}
	assert shown == {job: str(status) for job, status in expected.items()}
	assert shown[jobs["mixed success + failed"]] == "partial"


def test_job_status_breakdown_counts_jobs(jobs, session_scope):
	"""Job Status Breakdown counts jobs by Results' status, not device results."""
	expected = Counter(str(s) for s in statuses_by_results(session_scope, jobs).values())
	with session_scope() as s:
		rows = s.execute(text(panel_sql("Operations/operations_overview.json",
		                                "Job Status Breakdown"))).all()
	assert dict(rows) == dict(expected) and sum(expected.values()) == len(JOBS)


def test_devices_today_counts_devices(jobs, session_scope):
	"""Devices Today counts each device (address and port) once, however many
	jobs it was in today."""
	with session_scope() as s:
		s.add(DeviceResult(user_id=s.query(DeviceResult.user_id).first()[0], job_id=uuid.uuid4(),
		                   started_at=dt.datetime.now(), completed_at=dt.datetime.now(),
		                   device_ip="10.0.0.1", device_port=22, device_type="cisco_ios",
		                   commands_sent=1, status="success"))     # a device twice today
		shown = s.execute(text(panel_sql("Operations/operations_overview.json", "Devices Today"))).scalar()
	assert shown == sum(len(statuses) for statuses in JOBS.values())


def test_avg_job_duration_averages_jobs(jobs, session_scope):
	"""Avg Job Duration Over Time averages jobs' durations (first device's
	start to last device's end), not each device's push."""
	durations = [10 * (len(statuses) - 1) + 5 for statuses in JOBS.values()]
	with session_scope() as s:
		rows = s.execute(text(panel_sql("Operations/job_analytics.json",
		                                "Avg Job Duration Over Time"))).all()
	assert [round(float(avg), 3) for _, avg in rows] == [round(sum(durations) / len(durations), 3)]


def test_panel_titles_are_clean():
	"""No panel title has a typo we fixed, or leading/trailing spaces."""
	titles = [el["spec"].get("title", "") for path in DASHBOARDS.rglob("*.json")
	          for el in json.loads(path.read_text(encoding="utf-8"))["spec"]["elements"].values()]
	assert "Jon Duration Distribution" not in titles
	assert [t for t in titles if t != t.strip()] == []
