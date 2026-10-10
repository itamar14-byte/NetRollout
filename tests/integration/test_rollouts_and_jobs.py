"""Rollout routes (captured, never executed), cancel, SSE stream, rollback,
results / Verify Diff / log download, dashboards, operator analytics."""
import datetime as dt
import io
import itertools
import json
import os
import re
import threading
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from src import runtime
from src.db.settings import SETTINGS
from src.db.tables import AuditLog, DeviceResult, JobMetadata
from src.jobs import JOB_STATUSES, Draining, RolloutJob, job_status

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

CONFIG_SNAPSHOT_RETENTION_DAYS = SETTINGS["config_snapshot_retention_days"].default


@pytest.fixture
def operator(make_user, make_profile, make_device):
	"""An operator with one security profile and three devices: a cisco_ios
	(with a hostname variable), an arista_eos, and one without a profile."""
	user = make_user()
	prof = make_profile(user)
	ios = make_device(user, ip="10.0.0.1", profile_id=prof,
	                  var_maps={"hostname": "r1"})
	eos = make_device(user, ip="10.0.0.2", profile_id=prof,
	                  device_type="arista_eos")
	bare = make_device(user, ip="10.0.0.3")  # no security profile
	return SimpleNamespace(user=user, ios=ios, eos=eos, bare=bare)


def add_result(session_scope, user, job_id, ip="10.0.0.1", status="success",
               verified=None, config=None, age_days=0, commands=None, port=22):
	"""Store a device result for a job (and its job metadata when commands are
	given), dated age_days ago."""
	when = dt.datetime.now() - dt.timedelta(days=age_days)
	with session_scope() as s:
		s.add(DeviceResult(user_id=user.id, job_id=job_id, started_at=when,
		                   completed_at=when, device_ip=ip, device_port=port,
		                   device_type="cisco_ios", commands_sent=2,
		                   commands_verified=verified, fetched_config=config,
		                   status=status))
		if commands is not None:
			s.add(JobMetadata(job_id=job_id, user_id=user.id, commands=commands))


# ── Starting rollouts ────────────────────────────────────────────────────────

def test_single_platform_rollout_is_submitted(operator, client_for,
                                              captured_submits):
	"""A rollout to one platform is submitted once (redirect to Active Jobs) with
	the commands minus blank lines, verify on, the comment and the device."""
	resp = client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "hostname r9\n\n",
		"_verify": "on", "comment": "chg-1"})
	assert resp.status_code == 302 and "/active_jobs?new=" in resp.headers["Location"]
	(call,) = captured_submits
	assert call.commands == ["hostname r9"]
	assert call.params.verify is True and call.comment == "chg-1"
	assert [d.ip for d in call.devices] == ["10.0.0.1"]


def test_a_commands_file_saved_with_a_bom_sends_the_clean_first_command(
		operator, client_for, captured_submits):
	"""A commands file saved as "UTF-8 with BOM" (Windows Notepad) is read like
	the CLI reads it: the BOM is not part of the first command, blank lines
	are dropped."""
	resp = client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios)],
		"commands_file": (io.BytesIO("hostname r9\r\n\r\nntp server 1.1.1.1\r\n"
		                             .encode("utf-8-sig")), "cmds.txt")},
		content_type="multipart/form-data")
	assert resp.status_code == 302 and "/active_jobs?new=" in resp.headers["Location"]
	(call,) = captured_submits
	assert call.commands == ["hostname r9", "ntp server 1.1.1.1"]


def test_multi_platform_rollout_submits_one_job_per_platform(
		operator, client_for, captured_submits):
	"""A rollout over two platforms submits one job per platform, each with its
	own commands."""
	client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios), str(operator.eos)],
		"platform_commands": json.dumps({"cisco_ios": "hostname a",
		                                 "arista_eos": "hostname b"})})
	by_platform = {c.devices[0].device_type: c.commands for c in captured_submits}
	assert by_platform == {"cisco_ios": ["hostname a"],
	                       "arista_eos": ["hostname b"]}


def test_a_platform_without_commands_starts_nothing(operator, client_for,
                                                   captured_submits):
	"""A multi-platform rollout where one platform has no commands is refused
	as a whole: no job for the other platform either (arista_eos is
	submitted first - the platforms go in name order)."""
	resp = client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios), str(operator.eos)],
		"platform_commands": json.dumps({"arista_eos": "hostname b",
		                                 "cisco_ios": "  "})})
	assert resp.headers["Location"] == "/rollout/new"
	assert captured_submits == []


def test_a_stop_between_two_platforms_leaves_nothing_queued(
		app, operator, client_for, captured_submits, monkeypatch, session_scope):
	"""When NetRollout starts stopping between two platforms' jobs, the job
	already queued is cancelled, the start is refused, and no rollout.start
	is audited."""
	queued = []

	def submit(devices, commands, params, user_id, comment=None):
		if queued:
			raise Draining()
		queued.append(uuid.uuid4())
		return queued[0]
	cancelled = []
	monkeypatch.setattr(app.orchestrator, "submit", submit)
	monkeypatch.setattr(app.orchestrator, "cancel", cancelled.append)
	resp = client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios), str(operator.eos)],
		"platform_commands": json.dumps({"arista_eos": "hostname b",
		                                 "cisco_ios": "hostname a"})})
	assert resp.headers["Location"] == "/rollout/new"
	assert cancelled == queued and len(queued) == 1
	with session_scope() as s:
		assert not s.query(AuditLog).filter_by(action="rollout.start").count()


@pytest.mark.parametrize("form_fn", [
	lambda o: {"manual_commands": "x"},                                # no devices
	lambda o: {"device_ids": [str(o.ios)]},                            # no commands
	lambda o: {"device_ids": [str(o.bare)], "manual_commands": "x"},   # no profile
	lambda o: {"device_ids": ["not-a-uuid"], "manual_commands": "x"},
])
def test_invalid_rollouts_are_refused(operator, client_for, captured_submits,
                                      form_fn):
	"""An invalid rollout goes back to /rollout/new and submits nothing (cases:
	no devices, no commands, a device without a security profile, a bad id)."""
	resp = client_for(operator.user).post("/rollout/start",
	                                      data=form_fn(operator))
	assert resp.headers["Location"] == "/rollout/new"
	assert captured_submits == []


def test_same_target_selected_twice_is_refused(operator, client_for,
                                               make_user, make_profile,
                                               make_device, captured_submits):
	"""The operator's own entry and a global entry for the same box are refused
	together: nothing submitted, the flash names the ip:port and the label."""
	admin = make_user(role="admin")
	shared = make_device(admin, ip="10.0.0.1", label="CORE-GLOBAL",
	                     is_global=True, profile_id=make_profile(admin))
	client = client_for(operator.user)
	resp = client.post("/rollout/start", data={
		"device_ids": [str(operator.ios), str(shared)],
		"manual_commands": "hostname x"})
	assert resp.headers["Location"] == "/rollout/new"
	assert captured_submits == []
	with client.session_transaction() as s:
		(category, message), = s["_flashes"]
	assert "10.0.0.1:22" in message and "CORE-GLOBAL" in message


def test_same_ip_on_different_ports_is_allowed(operator, client_for,
                                               make_profile, make_device,
                                               captured_submits):
	"""Port-forwarded lab nodes (same host IP, different SSH ports) go in one
	job as two devices."""
	prof = make_profile(operator.user, label="lab")
	node_a = make_device(operator.user, ip="10.9.9.9", port=2001,
	                     label="node-a", profile_id=prof)
	node_b = make_device(operator.user, ip="10.9.9.9", port=2002,
	                     label="node-b", profile_id=prof)
	client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(node_a), str(node_b)], "manual_commands": "x"})
	(call,) = captured_submits
	assert sorted(d.endpoint for d in call.devices) == \
	       ["10.9.9.9:2001", "10.9.9.9:2002"]


def test_cannot_roll_out_to_another_users_device(operator, client_for,
                                                 make_user, captured_submits):
	"""Another user's device id is refused: back to /rollout/new, nothing
	submitted."""
	resp = client_for(make_user()).post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "x"})
	assert resp.headers["Location"] == "/rollout/new"
	assert captured_submits == []


# ── Cancel, stream, rollback ─────────────────────────────────────────────────

class FakeRunningJob:
	"""A stand-in for a running job in the orchestrator: cancel sets an event;
	its live log (follow_log) is one history line, then the given messages
	("__done__" ends it), then a heartbeat (None) while `over` says no -
	`heartbeats` at most, then the job leaves the orchestrator (`on_end`)."""
	def __init__(self, user_id, messages=(), heartbeats=0, on_end=None):
		self.job_id = uuid.uuid4()
		self.user_id = user_id
		self.started_at = dt.datetime.now()
		self.cancelled = threading.Event()
		self._messages = list(messages)
		self._heartbeats = heartbeats
		self._on_end = on_end

	claim = RolloutJob.claim   # the real one: a cancel claims through the job

	def cancel(self):
		self.cancelled.set()

	def is_over(self):
		return False

	def get_device_count(self):
		return 1

	def follow_log(self, over):
		yield "history-line"
		for message in self._messages:
			if message == "__done__":
				return
			yield message
		while not over():
			if self._heartbeats == 0:
				if not self._on_end:
					return
				self._on_end()
				continue
			self._heartbeats -= 1
			yield None


def test_owner_cancels_running_job(app, operator, client_for, monkeypatch):
	"""The job's owner cancels it: ok, the job's cancel is called and its Redis
	status is "cancelling"."""
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	_register_job_meta(app, operator.user, job.job_id)   # a running job's hash
	resp = client_for(operator.user, xhr=True).post(
		"/rollout/cancel", data={"job_id": str(job.job_id)})
	assert resp.json["status"] == "ok" and job.cancelled.is_set()
	status = app.backend.redis.client.hget(f"job:{job.job_id}:meta", "status")
	assert status == b"cancelling"


def test_other_user_cannot_cancel(app, operator, client_for, make_user,
                                  monkeypatch):
	"""Another user's cancel gets 404 "job not found" - the same answer as a job
	nobody knows, so it doesn't reveal that the job exists - and the job isn't
	cancelled."""
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	client = client_for(make_user(), xhr=True)
	resp = client.post("/rollout/cancel", data={"job_id": str(job.job_id)})
	assert resp.status_code == 404 and not job.cancelled.is_set()
	assert resp.json == {"status": "error", "message": "job not found"}
	unknown = client.post("/rollout/cancel", data={"job_id": str(uuid.uuid4())})
	assert (unknown.status_code, unknown.json) == (resp.status_code, resp.json)


def test_cancel_from_a_page_form_flashes_and_goes_back(app, operator,
                                                      client_for, monkeypatch):
	"""A page's Cancel form (no XHR header) gets the page it came from back
	with the outcome flashed, not JSON: the job is cancelled."""
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	client = client_for(operator.user)
	resp = client.post("/rollout/cancel", data={"job_id": str(job.job_id)},
	                   headers={"Referer": "https://localhost/dashboard"})
	assert resp.status_code == 302 and job.cancelled.is_set()
	assert resp.headers["Location"] == "/dashboard"
	page = client.get("/dashboard").get_data(as_text=True)
	assert "Rollout cancelled - devices it has not reached are skipped." in page


@pytest.mark.parametrize("referer, back", [
	# Active Jobs' auto-reload address: its ?_bg=1 would mark the next page as
	# background (not activity) - only the path is returned to
	("https://localhost/active_jobs?_bg=1", "/active_jobs"),
	("https://evil.example/active_jobs", "/active_jobs"),     # another site: never
	("https://localhost/dashboard?x=1", "/dashboard"),
])
def test_cancel_goes_back_to_the_page_not_its_query_nor_another_site(
		app, operator, client_for, monkeypatch, referer, back):
	"""A page's Cancel goes back to the referring page's path on this site - without its
	query, never to another site - else Active Jobs."""
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	resp = client_for(operator.user).post(
		"/rollout/cancel", data={"job_id": str(job.job_id)}, headers={"Referer": referer})
	assert resp.status_code == 302 and resp.headers["Location"] == back


def test_cancelling_a_queued_job_from_a_page_says_it_never_started(
		app, operator, client_for, monkeypatch):
	"""A queued job cancelled from a page's form: the flash says it never started - not
	"devices it has not reached are skipped" (it reached none)."""
	job = FakeRunningJob(operator.user.id)
	job.started_at = None                       # still queued
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	cancelled = []
	monkeypatch.setattr(app.orchestrator, "cancel", cancelled.append)
	client = client_for(operator.user)
	client.post("/rollout/cancel", data={"job_id": str(job.job_id)})
	assert cancelled == [job.job_id]
	page = client.get("/active_jobs").get_data(as_text=True)
	assert "Queued rollout cancelled - it never started." in page


def test_cancel_refused_from_a_page_form_flashes_the_reason(
		app, operator, client_for, make_user, monkeypatch):
	"""A refused Cancel from a page's form (another user's job; a job that
	has ended) is flashed and goes back - to Active Jobs without a referrer -
	and the job isn't cancelled."""
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	other = client_for(make_user())
	resp = other.post("/rollout/cancel", data={"job_id": str(job.job_id)})
	assert resp.status_code == 302 and not job.cancelled.is_set()
	assert resp.headers["Location"] == "/active_jobs"
	page = other.get("/active_jobs").get_data(as_text=True)
	assert "The rollout could not be cancelled: job not found." in page
	gone = client_for(operator.user).post(
		"/rollout/cancel", data={"job_id": str(uuid.uuid4())})
	assert gone.status_code == 302


def test_cancel_from_a_script_of_an_ended_job_answers_json_404(operator,
                                                               client_for):
	"""A script's Cancel (XHR) of a job that's no longer running gets JSON
	404, as the rollouts table expects."""
	resp = client_for(operator.user, xhr=True).post(
		"/rollout/cancel", data={"job_id": str(uuid.uuid4())})
	assert resp.status_code == 404 and resp.json["status"] == "error"


def test_stream_replays_history_then_tails_live(app, operator, client_for,
                                                monkeypatch):
	"""The live log stream sends the history before the live messages and ends
	with the done event."""
	job = FakeRunningJob(operator.user.id, messages=["live-line", "__done__"])
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	body = client_for(operator.user).get(
		f"/rollout/stream/{job.job_id}").get_data(as_text=True)
	assert body.index("data: history-line") < body.index("data: live-line")
	assert body.rstrip().endswith("event: done\ndata:")


def test_stream_of_another_users_job_not_found(app, operator, client_for,
                                               make_user, monkeypatch):
	"""Another user's live log stream gets 404 with no body - the same answer
	as a job nobody knows."""
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	client = client_for(make_user())
	resp = client.get(f"/rollout/stream/{job.job_id}")
	assert resp.status_code == 404 and resp.get_data() == b""
	unknown = client.get(f"/rollout/stream/{uuid.uuid4()}")
	assert (unknown.status_code, unknown.get_data()) == (404, b"")


def test_stream_of_a_queued_job_waits_for_it(app, operator, client_for, monkeypatch):
	"""A queued job's live log says it's queued (once, first), then waits with
	heartbeat comments instead of ending at once; it ends with "done" when the
	job leaves the orchestrator."""
	job = FakeRunningJob(operator.user.id, heartbeats=3,
	                     on_end=lambda: app.orchestrator._jobs.pop(job.job_id, None))
	job.started_at = None                       # queued: no thread yet
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	body = client_for(operator.user).get(
		f"/rollout/stream/{job.job_id}").get_data(as_text=True)
	assert body.startswith("data: Queued — waiting for a free slot\n\n")
	assert body.count("Queued — waiting") == 1
	assert body.count(": hb\n\n") == 3
	assert body.endswith("event: done\ndata: \n\n")


def test_stream_sends_a_multi_line_message_whole(app, operator, client_for, monkeypatch):
	"""A message with newlines (a device's error output, \\r\\n too) is one SSE
	message: a data line per line, none cut off."""
	job = FakeRunningJob(operator.user.id,
	                     messages=["Error:\r\n% Invalid input\nat line 2", "__done__"])
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	body = client_for(operator.user).get(
		f"/rollout/stream/{job.job_id}").get_data(as_text=True)
	assert "data: Error:\ndata: % Invalid input\ndata: at line 2\n\n" in body


def test_stream_heartbeat_is_a_comment(app, operator, client_for, monkeypatch):
	"""The heartbeat is an SSE comment, not an empty message."""
	job = FakeRunningJob(operator.user.id, heartbeats=2,
	                     on_end=lambda: app.orchestrator._jobs.pop(job.job_id, None))
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	body = client_for(operator.user).get(
		f"/rollout/stream/{job.job_id}").get_data(as_text=True)
	assert body == ("data: history-line\n\n: hb\n\n: hb\n\n"
	                "event: done\ndata: \n\n")


def test_stream_of_a_job_that_has_ended(app, operator, client_for, make_user,
                                        session_scope):
	"""A job no longer running here (it just finished): its owner and an admin
	get a stream with only the done event (the page shows the outcome);
	another user 404, as a job nobody knows."""
	job_id = uuid.uuid4()
	with session_scope() as s:
		s.add(JobMetadata(job_id=job_id, user_id=operator.user.id, commands=["x"]))
	for user in (operator.user, make_user(role="admin")):
		resp = client_for(user).get(f"/rollout/stream/{job_id}")
		assert resp.status_code == 200 and resp.mimetype == "text/event-stream"
		assert resp.get_data(as_text=True) == "event: done\ndata: \n\n"
	other = client_for(make_user()).get(f"/rollout/stream/{job_id}")
	assert other.status_code == 404 and other.get_data() == b""
	assert client_for(operator.user).get(
		f"/rollout/stream/{uuid.uuid4()}").status_code == 404


def test_a_multi_platform_start_failing_on_its_second_platform_cancels_the_first(
		app, operator, client_for, monkeypatch, session_scope):
	"""Queuing the second platform's job fails (a service error, not a stop):
	the first platform's job is cancelled - none or all - and no rollout.start
	is audited."""
	queued = []

	def submit(devices, commands, params, user_id, comment=None):
		if queued:
			raise RuntimeError("simulated: Redis went away")
		queued.append(uuid.uuid4())
		return queued[0]
	cancelled = []
	monkeypatch.setattr(app.orchestrator, "submit", submit)
	monkeypatch.setattr(app.orchestrator, "cancel", cancelled.append)
	with pytest.raises(RuntimeError, match="simulated"):   # the test app propagates it
		client_for(operator.user).post("/rollout/start", data={
			"device_ids": [str(operator.ios), str(operator.eos)],
			"platform_commands": json.dumps({"arista_eos": "hostname b",
			                                 "cisco_ios": "hostname a"})})
	assert cancelled == queued and len(queued) == 1
	with session_scope() as s:
		assert not s.query(AuditLog).filter_by(action="rollout.start").count()


def test_rollback_targets_successful_devices_once_per_ip(
		operator, client_for, session_scope, make_user, make_profile,
		make_device, captured_submits):
	"""A rollback is submitted for the job's successful devices only, each once:
	the operator's own device, not a global device sharing its IP."""
	admin = make_user(role="admin")
	make_device(admin, ip="10.0.0.1", label="GLOBAL-SAME-IP", is_global=True,
	            profile_id=make_profile(admin))
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	add_result(session_scope, operator.user, job, ip="10.0.0.2", status="failed")
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}",
	                                      json={"commands": "no hostname"})
	assert resp.json["status"] == "ok"
	(call,) = captured_submits
	assert [d.label for d in call.devices] == ["dev-10.0.0.1"]  # own, once


def test_rollback_to_a_device_without_a_profile_is_refused_in_words(
		operator, client_for, session_scope, captured_submits):
	"""A rollback whose device has no security profile any more is refused
	with a reason (409), not a server error; nothing is submitted."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.3")   # operator.bare
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}",
	                                      json={"commands": "no hostname"})
	assert resp.status_code == 409
	assert "no security profile" in resp.json["message"]
	assert captured_submits == []


def test_rollback_matches_on_ip_and_port(operator, client_for, session_scope,
                                         make_profile, make_device,
                                         captured_submits):
	"""A rollback picks devices by ip and port: of two nodes on one IP only the
	successful one (port 2001) is targeted."""
	prof = make_profile(operator.user, label="lab")
	make_device(operator.user, ip="10.9.9.9", port=2001, label="node-a",
	            profile_id=prof)
	make_device(operator.user, ip="10.9.9.9", port=2002, label="node-b",
	            profile_id=prof)
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.9.9.9", port=2001)
	add_result(session_scope, operator.user, job, ip="10.9.9.9", port=2002,
	           status="failed")
	client_for(operator.user).post(f"/rollout/rollback/{job}",
	                               json={"commands": "no hostname"})
	(call,) = captured_submits
	assert [d.label for d in call.devices] == ["node-a"]  # not the failed one


def _rollback_audit(session_scope, job):
	"""The rollout.rollback audit row of a job: (actor, detail)."""
	with session_scope() as s:
		(row,) = s.query(AuditLog).filter_by(action="rollout.rollback",
		                                     object_id=job).all()
		return row.actor_username, row.detail


def test_operator_rolls_back_own_job_audited_with_owner(
		operator, client_for, session_scope, captured_submits):
	"""An operator's rollback of their own job is submitted as them to their
	device, and the audit names the job's owner (them)."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}",
	                                      json={"commands": "no hostname"})
	assert resp.json["status"] == "ok"
	(call,) = captured_submits
	assert call.user_id == operator.user.id
	assert [d.label for d in call.devices] == ["dev-10.0.0.1"]
	actor, detail = _rollback_audit(session_scope, job)
	assert actor == operator.user.username
	assert detail["job_owner"] == operator.user.username
	assert detail["job_owner_id"] == str(operator.user.id)


def test_operator_cannot_roll_back_another_users_job(
		operator, client_for, session_scope, make_user, captured_submits):
	"""Another operator's job is answered 404 (as its summary is), nothing
	submitted."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	resp = client_for(make_user()).post(f"/rollout/rollback/{job}",
	                                    json={"commands": "no hostname"})
	assert resp.status_code == 404
	assert captured_submits == []


def test_admin_rolls_back_another_users_job_on_the_owners_devices(
		operator, client_for, session_scope, make_user, make_profile,
		make_device, make_mapping, captured_submits):
	"""An admin's rollback of an operator's job targets the operator's device
	(not the admin's own entry on the same ip:port) with the operator's
	variable mappings (not the admin's), is submitted as the admin, and the
	audit names the operator as the job's owner."""
	admin = make_user(role="admin")
	make_device(admin, ip="10.0.0.1", label="ADMIN-OWN",
	            profile_id=make_profile(admin))
	make_mapping(operator.user, token="HOST", prop="hostname",
	             devices=[operator.ios])
	make_mapping(admin, token="ADM", prop="site", devices=[operator.ios])
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	add_result(session_scope, operator.user, job, ip="10.0.0.2", status="failed")
	resp = client_for(admin).post(f"/rollout/rollback/{job}",
	                              json={"commands": "no hostname"})
	assert resp.json["status"] == "ok"
	(call,) = captured_submits
	assert call.user_id == admin.id
	(device,) = call.devices
	assert device.label == "dev-10.0.0.1"
	assert device.var_map_subs == {"$$HOST$$": ("hostname", None)}
	actor, detail = _rollback_audit(session_scope, job)
	assert actor == admin.username
	assert detail["job_owner"] == operator.user.username
	assert detail["job_owner_id"] == str(operator.user.id)
	assert detail["new_job_id"] == str(call.job_id)


def test_rollback_of_a_job_without_successful_devices_is_refused(
		operator, client_for, session_scope, captured_submits):
	"""A job none of whose devices succeeded is refused with the reason;
	nothing submitted."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1", status="failed")
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}",
	                                      json={"commands": "no hostname"})
	assert resp.status_code == 400
	assert resp.json["message"] == \
		"No successfully configured devices found for this job."
	assert captured_submits == []


def test_results_offer_rollback_only_with_a_successful_device(
		operator, client_for, session_scope):
	"""Results show Rollback (opening the shared dialog) on a job with a
	successful device, not on a job whose devices all failed."""
	good, bad = uuid.uuid4(), uuid.uuid4()
	add_result(session_scope, operator.user, good, ip="10.0.0.1")
	add_result(session_scope, operator.user, good, ip="10.0.0.2", status="failed")
	add_result(session_scope, operator.user, bad, ip="10.0.0.1", status="failed")
	html = client_for(operator.user).get("/results").get_data(as_text=True)
	assert f'data-rollback-job="{good}"' in html
	assert f'data-rollback-job="{bad}"' not in html
	assert 'id="rollbackModal"' in html


def test_results_offer_an_admin_rollback_of_another_users_job(
		operator, client_for, session_scope, make_user):
	"""An admin's Results show Rollback on another user's finished job."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	html = client_for(make_user(role="admin")).get("/results?view=all")\
		.get_data(as_text=True)
	assert f'data-rollback-job="{job}"' in html


# ── Results / Verify Diff / logs ─────────────────────────────────────────────

def test_results_distinguish_devices_sharing_an_ip(operator, client_for,
                                                   session_scope, make_profile,
                                                   make_device):
	"""Results tell apart devices on one IP: a label by its own ip:port, an
	unlabelled one as ip:port, and Verify Diff serves each port's config."""
	prof = make_profile(operator.user, label="lab")
	make_device(operator.user, ip="10.9.9.9", port=2001, label="node-a",
	            profile_id=prof)
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.9.9.9", port=2001,
	           status="partial", verified=1, config="cfg-a", commands=["a", "b"])
	add_result(session_scope, operator.user, job, ip="10.9.9.9", port=2002,
	           status="partial", verified=1, config="cfg-b", commands=["a", "b"])
	client = client_for(operator.user)
	html = client.get("/results").get_data(as_text=True)
	assert "node-a" in html            # labelled via its own ip:port
	assert "10.9.9.9:2002" in html     # unlabelled: falls back to ip:port
	a = client.get(f"/results/config_diff/{job}/10.9.9.9?port=2001")
	b = client.get(f"/results/config_diff/{job}/10.9.9.9?port=2002")
	assert (a.json["config"], b.json["config"]) == ("cfg-a", "cfg-b")

def test_results_write_an_ipv6_device_in_brackets(operator, client_for, session_scope):
	"""An unlabelled IPv6 device shows as [address]:port on Results, and Verify
	Diff serves its config with the address in the URL."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="2001:db8::7", port=2222,
	           status="partial", verified=1, config="cfg-v6", commands=["a", "b"])
	client = client_for(operator.user)
	assert "[2001:db8::7]:2222" in client.get("/results").get_data(as_text=True)
	diff = client.get(f"/results/config_diff/{job}/2001:db8::7?port=2222")
	assert diff.json["config"] == "cfg-v6"


def test_results_show_verify_diff_and_expired_states(operator, client_for,
                                                     session_scope):
	"""A job with a stored config gets Verify Diff; one older than the snapshot
	retention shows "Verify Diff expired" once; no config is in the page."""
	live, stale = uuid.uuid4(), uuid.uuid4()
	add_result(session_scope, operator.user, live, status="partial", verified=1,
	           config="cfg-live", commands=["a", "b"])
	add_result(session_scope, operator.user, stale, status="partial",
	           verified=1, age_days=CONFIG_SNAPSHOT_RETENTION_DAYS + 2,
	           commands=["a", "b"])
	html = client_for(operator.user).get("/results").get_data(as_text=True)
	assert f'data-job-id="{live}"' in html
	assert html.count("Verify Diff expired") == 1
	assert "cfg-live" not in html  # config no longer shipped with the page


def test_config_diff_endpoint(operator, client_for, session_scope, make_user):
	"""Verify Diff's endpoint returns the config and commands to the owner, 410
	when the config is gone, 404 to another user - the same answer as a job that
	doesn't exist, as the summary and rollback give (it doesn't reveal the job)."""
	job, cleared = uuid.uuid4(), uuid.uuid4()
	add_result(session_scope, operator.user, job, status="partial", verified=1,
	           config="running-cfg", commands=["a", "b"])
	add_result(session_scope, operator.user, cleared, status="partial",
	           verified=1, commands=["a", "b"])
	client = client_for(operator.user)
	ok = client.get(f"/results/config_diff/{job}/10.0.0.1")
	assert ok.json["config"] == "running-cfg" and ok.json["commands"] == ["a", "b"]
	assert client.get(f"/results/config_diff/{cleared}/10.0.0.1").status_code == 410
	other = client_for(make_user()).get(f"/results/config_diff/{job}/10.0.0.1")
	assert other.status_code == 404 and other.json["message"] == "Not found"


def test_config_diff_returns_the_engines_verdicts(operator, client_for,
                                                  session_scope):
	"""The page shows the server's matcher (sections, removals, variables), not
	a text search of its own: each command comes back with its verdict."""
	job = uuid.uuid4()
	config = ("interface GigabitEthernet1\n description core\n!\n"
	          "interface GigabitEthernet2\n shutdown\n!\n")
	commands = ["interface GigabitEthernet1", "description core",
	            "interface GigabitEthernet2", "no shutdown",
	            "description $$DESC$$", "write memory"]
	add_result(session_scope, operator.user, job, status="partial",
	           verified=3, config=config, commands=commands)
	resp = client_for(operator.user).get(f"/results/config_diff/{job}/10.0.0.1")
	assert resp.json["verdicts"] == [
		["interface GigabitEthernet1", "verified"],
		["description core", "verified"],
		["interface GigabitEthernet2", "verified"],
		["no shutdown", "still configured"],
		["description $$DESC$$", "variable"],
		["write memory", "not verifiable"]]


def test_results_page_shows_what_needs_a_person(operator, client_for,
                                                session_scope):
	"""A device needing a person shows on Results without reading the log: one
	action-needed badge (on that job only), "Action needed on" and the
	device's instruction."""
	job, clean = uuid.uuid4(), uuid.uuid4()
	now = dt.datetime.now()
	with session_scope() as s:
		s.add(DeviceResult(user_id=operator.user.id, job_id=job, started_at=now,
		                   completed_at=now, device_ip="10.0.0.1", device_port=22,
		                   device_type="checkpoint_gaia", commands_sent=1,
		                   status="success",
		                   action_needed="the change is live but NOT saved — "
		                                 "save it on the device (config lock)"))
	add_result(session_scope, operator.user, clean, ip="10.0.0.2")
	html = client_for(operator.user).get("/results").get_data(as_text=True)
	assert html.count('class="action-needed-badge"') == 1      # only that job
	assert "Action needed on" in html
	assert "save it on the device (config lock)" in html


def add_jobs(session_scope, user, count):
	"""Store count one-device jobs, a minute apart; :returns: their ids,
	newest first."""
	now = dt.datetime.now()
	jobs = [uuid.uuid4() for _ in range(count)]
	with session_scope() as s:
		s.add_all(DeviceResult(user_id=user.id, job_id=job,
		                       started_at=now - dt.timedelta(minutes=i),
		                       completed_at=now - dt.timedelta(minutes=i),
		                       device_ip="10.0.0.1", device_port=22,
		                       device_type="cisco_ios", commands_sent=1,
		                       status="success")
		          for i, job in enumerate(jobs))
	return jobs


def test_results_show_100_jobs_per_page_by_default(operator, client_for,
                                                    session_scope):
	"""105 jobs: the first page holds the newest 100, "Page 1 of 2" with a
	link to page 2, and the count says 105; page 2 holds the oldest 5."""
	jobs = add_jobs(session_scope, operator.user, 105)
	client = client_for(operator.user)
	html = client.get("/results").get_data(as_text=True)
	assert html.count('class="job-row"') == 100
	assert f'data-job-id="{jobs[0]}"' in html
	assert f'data-job-id="{jobs[100]}"' not in html
	assert "Page 1 of 2" in html
	assert 'href="/results?page=2"' in html
	assert ">105 jobs</span>" in html
	assert '<option value="100" selected>' in html
	page2 = client.get("/results?page=2").get_data(as_text=True)
	assert page2.count('class="job-row"') == 5
	assert f'data-job-id="{jobs[104]}"' in page2
	assert "Page 2 of 2" in page2
	assert ">105 jobs</span>" in page2


def test_results_per_page_choice_and_fallback(operator, client_for,
                                              session_scope):
	"""?per_page= takes one of the dropdown's choices (25: 105 jobs on 5
	pages); anything else - not a number, not a choice - is 100."""
	add_jobs(session_scope, operator.user, 105)
	client = client_for(operator.user)
	html = client.get("/results?per_page=25").get_data(as_text=True)
	assert html.count('class="job-row"') == 25
	assert "Page 1 of 5" in html
	assert '<option value="25" selected>' in html
	assert 'href="/results?per_page=25&amp;page=2"' in html  # keeps per_page
	for raw in ("abc", "37", "0", "-25"):
		html = client.get(f"/results?per_page={raw}").get_data(as_text=True)
		assert html.count('class="job-row"') == 100, raw
		assert "Page 1 of 2" in html, raw


def test_results_page_out_of_range_is_clamped(operator, client_for,
                                              session_scope):
	"""A page past the last shows the last; zero, negative or not a number
	shows the first."""
	jobs = add_jobs(session_scope, operator.user, 105)
	client = client_for(operator.user)
	html = client.get("/results?page=999").get_data(as_text=True)
	assert "Page 2 of 2" in html and html.count('class="job-row"') == 5
	assert f'data-job-id="{jobs[104]}"' in html
	for raw in ("0", "-3", "x"):
		html = client.get(f"/results?page={raw}").get_data(as_text=True)
		assert "Page 1 of 2" in html, raw
		assert f'data-job-id="{jobs[0]}"' in html, raw


def test_results_page_controls_only_when_needed(operator, client_for,
                                                session_scope):
	"""Jobs that fit the smallest choice: no pager at all. More than that but
	one page: the per-page dropdown, no page controls."""
	add_jobs(session_scope, operator.user, 3)
	client = client_for(operator.user)
	html = client.get("/results").get_data(as_text=True)
	assert html.count('class="job-row"') == 3
	assert 'class="results-pager' not in html
	assert 'name="per_page"' not in html and "Page 1 of" not in html
	add_jobs(session_scope, operator.user, 27)  # 30 in all
	html = client.get("/results").get_data(as_text=True)
	assert html.count('class="job-row"') == 30
	assert 'name="per_page"' in html
	assert "Page 1 of" not in html and "page=2" not in html


def test_results_job_link_lands_on_its_page(operator, client_for,
                                            session_scope):
	"""/results?job=<id> (the completion card's and dashboard's link) shows
	the page that job is on; an explicit ?page= wins."""
	jobs = add_jobs(session_scope, operator.user, 105)
	client = client_for(operator.user)
	html = client.get(f"/results?job={jobs[102]}").get_data(as_text=True)
	assert "Page 2 of 2" in html
	assert f'data-job-id="{jobs[102]}"' in html
	assert 'href="/results?page=1"' in html  # the link drops ?job=
	html = client.get(f"/results?job={jobs[3]}").get_data(as_text=True)
	assert "Page 1 of 2" in html and f'data-job-id="{jobs[3]}"' in html
	html = client.get(f"/results?job={jobs[102]}&page=1").get_data(as_text=True)
	assert "Page 1 of 2" in html
	assert f'data-job-id="{jobs[102]}"' not in html


def test_results_admin_pages_other_users_apart(make_user, client_for,
                                               session_scope):
	"""An admin's own jobs and other users' are paged apart (?page /
	?other_page), the counts over all of them, and the all-users view kept
	in the links."""
	admin, other = make_user(role="admin"), make_user()
	add_jobs(session_scope, admin, 3)
	theirs = add_jobs(session_scope, other, 30)
	client = client_for(admin)
	html = client.get("/results?per_page=25&view=all").get_data(as_text=True)
	assert 'data-mine-total="3" data-all-total="33"' in html
	assert html.count('class="job-row"') == 3 * 2 + 25  # own: flat + split
	assert "Page 1 of 2" in html
	assert 'href="/results?per_page=25&amp;view=all&amp;other_page=2"' in html
	assert "toggleAllUsers();" in html  # opens on the all-users view
	page2 = client.get("/results?per_page=25&view=all&other_page=2")\
		.get_data(as_text=True)
	assert page2.count('class="job-row"') == 3 * 2 + 5
	assert f'data-job-id="{theirs[29]}"' in page2
	assert f'data-job-id="{theirs[0]}"' not in page2
	assert ">3 jobs</span>" in page2
	add_jobs(session_scope, admin, 27)  # 30 own: each view its own dropdown
	html = client.get("/results?view=all").get_data(as_text=True)
	assert html.count('id="perPage-page"') == 1
	assert html.count('id="perPage-page-all"') == 1


def test_results_admin_sees_the_owner_of_another_users_job(make_user,
                                                           client_for,
                                                           session_scope):
	"""An admin's all-users view names the owner on each other user's job."""
	admin, other = make_user(role="admin"), make_user()
	add_jobs(session_scope, other, 1)
	html = client_for(admin).get("/results?view=all").get_data(as_text=True)
	assert f'<span class="owner-badge ms-2">{other.username}</span>' in html


def test_results_admin_job_link_lands_on_another_users_job(make_user,
                                                           client_for,
                                                           session_scope):
	"""An admin's /results?job=<id> of another user's job opens the all-users
	view on that job's page of the other users' list; an explicit
	?other_page= wins; a job in neither list: page 1, the flat view."""
	admin, other = make_user(role="admin"), make_user()
	add_jobs(session_scope, admin, 3)
	theirs = add_jobs(session_scope, other, 30)
	client = client_for(admin)
	html = client.get(f"/results?per_page=25&job={theirs[27]}")\
		.get_data(as_text=True)
	assert "toggleAllUsers();" in html  # opens on the all-users view
	assert "Page 2 of 2" in html
	assert f'data-job-id="{theirs[27]}"' in html
	assert f'data-job-id="{theirs[0]}"' not in html
	html = client.get(f"/results?per_page=25&job={theirs[27]}&other_page=1")\
		.get_data(as_text=True)
	assert "Page 1 of 2" in html
	assert f'data-job-id="{theirs[27]}"' not in html
	html = client.get(f"/results?per_page=25&job={uuid.uuid4()}")\
		.get_data(as_text=True)
	assert "toggleAllUsers();" not in html
	assert "Page 1 of 2" in html and "Page 2 of 2" not in html
	assert f'data-job-id="{theirs[27]}"' not in html


def test_results_admin_labels_load_only_the_pages_devices(make_user,
                                                          make_device,
                                                          client_for,
                                                          session_scope, app):
	"""An admin's device labels come from any user's inventory by ip:port, but
	only the shown devices' rows are read - never the whole inventory."""
	admin, other = make_user(role="admin"), make_user()
	make_device(other, ip="10.9.9.9", port=2001, label="their-node")
	make_device(other, ip="10.9.9.8", port=22, label="unshown-node")
	make_device(admin, ip="10.9.9.7", port=22, label="my-node")
	shown = dict(status="partial", verified=1, config="cfg")  # Verify Diff: labels
	add_result(session_scope, admin, uuid.uuid4(), ip="10.9.9.9", port=2001,
	           **shown)
	add_result(session_scope, other, uuid.uuid4(), ip="10.9.9.7", **shown)
	add_result(session_scope, other, uuid.uuid4(), ip="10.9.9.6", port=2222,
	           **shown)
	statements = []

	def capture(conn, cursor, statement, params, context, executemany):
		statements.append(statement)

	engine = app.backend.postgres.engine
	event.listen(engine, "before_cursor_execute", capture)
	try:
		html = client_for(admin).get("/results?view=all").get_data(as_text=True)
	finally:
		event.remove(engine, "before_cursor_execute", capture)
	assert "their-node" in html        # another user's device, own job
	assert "my-node" in html           # own device, another user's job
	assert "10.9.9.6:2222" in html     # unlabelled: falls back to ip:port
	assert "unshown-node" not in html
	reads = [s for s in statements if "FROM inventory" in s]
	assert reads and all("IN (" in s for s in reads), reads


def test_results_page_the_jobs_in_the_database(operator, client_for,
                                               session_scope, app):
	"""The jobs are paged by the query (LIMIT/OFFSET), and only the page's
	device results are loaded - never the whole table."""
	add_jobs(session_scope, operator.user, 105)
	statements = []

	def capture(conn, cursor, statement, params, context, executemany):
		statements.append(statement)

	engine = app.backend.postgres.engine
	event.listen(engine, "before_cursor_execute", capture)
	try:
		html = client_for(operator.user).get("/results?page=2")\
			.get_data(as_text=True)
	finally:
		event.remove(engine, "before_cursor_execute", capture)
	assert html.count('class="job-row"') == 5
	reads = [s for s in statements if "FROM device_results" in s]
	assert any("LIMIT" in s and "OFFSET" in s for s in reads)
	assert all("LIMIT" in s or "IN (" in s or "count(" in s
	           or "= %(job_id" in s for s in reads), reads


def add_status_jobs(session_scope, user, device_statuses, count=1):
	"""Store count jobs whose devices end with device_statuses (one device
	each), a minute apart and an hour older than add_jobs'; :returns: their
	ids, newest first."""
	now = dt.datetime.now() - dt.timedelta(hours=1)
	jobs = [uuid.uuid4() for _ in range(count)]
	with session_scope() as s:
		s.add_all(DeviceResult(user_id=user.id, job_id=job,
		                       started_at=now - dt.timedelta(minutes=i),
		                       completed_at=now - dt.timedelta(minutes=i),
		                       device_ip=f"10.0.1.{n}", device_port=22,
		                       device_type="cisco_ios", commands_sent=1,
		                       status=status)
		          for i, job in enumerate(jobs)
		          for n, status in enumerate(device_statuses))
	return jobs


def filter_hrefs(html, cls="filter-btn"):
	""":returns: {label: (href, active)} of the status filter's links"""
	return {label: (href.replace("&amp;", "&"), bool(active))
	        for active, href, label in re.findall(
		        rf'<a class="{cls}( active)?" href="([^"]*)">(\w+)</a>', html)}


def shown_jobs(html):
	""":returns: the ids of the job rows on the page"""
	return set(re.findall(r'class="job-row"[^>]*?data-job-id="([^"]+)"', html))


def test_results_status_filter_follows_job_status(operator, client_for,
                                                  session_scope):
	"""?status= shows exactly the jobs whose status (job_status, the badge
	the page shows) is that one - for every mix of 1 to 3 device outcomes;
	an unknown or empty value shows every job, "All" active."""
	outcomes = ("success", "partial", "failed", "cancelled")
	expected = {status: set() for status in JOB_STATUSES}
	for mix in itertools.chain.from_iterable(
			itertools.combinations_with_replacement(outcomes, n)
			for n in (1, 2, 3)):
		job, = add_status_jobs(session_scope, operator.user, mix)
		rows = [SimpleNamespace(status=s) for s in mix]
		expected[job_status(rows)].add(str(job))
	assert all(expected.values())  # every status has jobs
	client = client_for(operator.user)
	for status in JOB_STATUSES:
		html = client.get(f"/results?per_page=500&status={status}")\
			.get_data(as_text=True)
		assert shown_jobs(html) == expected[status], status
		assert filter_hrefs(html)[status.capitalize()][1], status
		assert not filter_hrefs(html)["All"][1], status
	every = set().union(*expected.values())
	for raw in ("bogus", "", "SUCCESS"):
		html = client.get(f"/results?per_page=500&status={raw}")\
			.get_data(as_text=True)
		assert shown_jobs(html) == every, raw
		links = filter_hrefs(html)
		assert links["All"][1], raw
		assert not any(active for label, (_, active) in links.items()
		               if label != "All"), raw


def test_results_status_filter_before_paging(operator, client_for,
                                             session_scope):
	"""The filter is applied before the pages: the count, the page count and
	each page hold only the matching jobs."""
	failed = add_status_jobs(session_scope, operator.user, ["failed"], 30)
	add_jobs(session_scope, operator.user, 5)  # newer, success
	client = client_for(operator.user)
	html = client.get("/results?per_page=25&status=failed")\
		.get_data(as_text=True)
	assert ">30 failed jobs</span>" in html
	assert "Page 1 of 2" in html
	assert shown_jobs(html) == {str(job) for job in failed[:25]}
	page2 = client.get("/results?per_page=25&status=failed&page=2")\
		.get_data(as_text=True)
	assert shown_jobs(page2) == {str(job) for job in failed[25:]}
	html = client.get("/results?status=cancelled").get_data(as_text=True)
	assert html.count('class="job-row"') == 0
	assert ">0 cancelled jobs</span>" in html
	assert "No cancelled jobs." in html
	assert "No completed jobs yet" not in html  # the filter bar stays


def test_results_count_says_it_is_filtered(make_user, client_for,
                                           session_scope):
	"""The header's job count names the filter ("1 failed job"), and gives it
	to the all-users view's count (written by the page's script); no filter:
	"N jobs" as ever."""
	admin, other = make_user(role="admin"), make_user()
	add_status_jobs(session_scope, admin, ["failed"])
	add_jobs(session_scope, admin, 2)
	add_status_jobs(session_scope, other, ["failed"], 3)
	client = client_for(admin)
	html = client.get("/results").get_data(as_text=True)
	assert ">3 jobs</span>" in html
	assert 'data-filter=""' in html
	html = client.get("/results?status=failed").get_data(as_text=True)
	assert ">1 failed job</span>" in html
	assert 'data-mine-total="1" data-all-total="4" data-filter="failed"' in html
	html = client.get("/results?status=cancelled").get_data(as_text=True)
	assert ">0 cancelled jobs</span>" in html


def test_results_status_filter_links(operator, client_for, session_scope):
	"""The filter's links keep ?per_page, start again at page 1 (?page and
	?job= dropped) and set ?status (All: none); the pages' links and the
	per-page form keep the filter."""
	jobs = add_status_jobs(session_scope, operator.user, ["failed"], 30)
	client = client_for(operator.user)
	html = client.get(
		f"/results?per_page=25&page=2&status=failed&job={jobs[0]}")\
		.get_data(as_text=True)
	assert filter_hrefs(html) == {
		"All": ("/results?per_page=25", False),
		"Success": ("/results?per_page=25&status=success", False),
		"Partial": ("/results?per_page=25&status=partial", False),
		"Failed": ("/results?per_page=25&status=failed", True),
		"Cancelled": ("/results?per_page=25&status=cancelled", False)}
	assert 'href="/results?per_page=25&amp;page=1&amp;status=failed"' in html
	assert '<input type="hidden" name="status" value="failed">' in html


def test_results_status_filter_in_an_admins_views(make_user, client_for,
                                                  session_scope):
	"""One filter for both of an admin's lists: own and other users' jobs
	filtered and counted alike; the all-users view's links keep ?view=all,
	the flat view's don't, and both drop ?other_page."""
	admin, other = make_user(role="admin"), make_user()
	mine = add_status_jobs(session_scope, admin, ["failed"], 2)
	add_jobs(session_scope, admin, 3)
	theirs = add_status_jobs(session_scope, other, ["success", "failed"], 4)
	add_status_jobs(session_scope, other, ["failed"], 6)
	client = client_for(admin)
	html = client.get("/results?view=all&other_page=2&status=partial")\
		.get_data(as_text=True)
	assert 'data-mine-total="0" data-all-total="4"' in html
	assert shown_jobs(html) == {str(job) for job in theirs}
	assert "No partial jobs." in html
	assert filter_hrefs(html, "filter-btn-split") == {
		"All": ("/results?view=all", False),
		"Success": ("/results?view=all&status=success", False),
		"Partial": ("/results?view=all&status=partial", True),
		"Failed": ("/results?view=all&status=failed", False),
		"Cancelled": ("/results?view=all&status=cancelled", False)}
	assert filter_hrefs(html)["Failed"] == ("/results?status=failed", False)
	html = client.get("/results?view=all&status=failed").get_data(as_text=True)
	assert 'data-mine-total="2" data-all-total="8"' in html
	assert html.count('class="job-row"') == 2 * 2 + 6  # own: flat + split
	assert {str(job) for job in mine} <= shown_jobs(html)
	assert not shown_jobs(html) & {str(job) for job in theirs}


def test_job_summary_for_the_completion_card(operator, client_for,
                                            session_scope, make_user):
	"""The completion card's summary: device count, counts per status, action
	needed by inventory label, job id, comment and results link; 404 when not
	stored yet or for another user, 200 for an admin."""
	job = uuid.uuid4()
	now = dt.datetime.now()
	with session_scope() as s:
		s.add(DeviceResult(user_id=operator.user.id, job_id=job, started_at=now,
		                   completed_at=now, device_ip="10.0.0.1", device_port=22,
		                   device_type="checkpoint_gaia", commands_sent=1,
		                   status="success",
		                   action_needed="the change is live but NOT saved"))
		s.add(JobMetadata(job_id=job, user_id=operator.user.id,
		                  commands=["set x"], comment="chg-42"))
	add_result(session_scope, operator.user, job, ip="10.0.0.2", status="failed")
	resp = client_for(operator.user).get(f"/results/summary/{job}")
	assert resp.status_code == 200
	body = resp.json
	assert body["device_count"] == 2
	assert body["counts"] == {"success": 1, "failed": 1}
	# devices by their inventory label
	assert body["action_needed"] == [{"device": "dev-10.0.0.1",
	                                  "text": "the change is live but NOT saved"}]
	# which job, when several finish in parallel
	assert body["job_id"] == str(job) and body["comment"] == "chg-42"
	assert body["results_url"] == f"/results?job={job}"
	# not stored yet → 404, so the card retries; other users can't see it
	assert client_for(operator.user).get(
		f"/results/summary/{uuid.uuid4()}").status_code == 404
	assert client_for(make_user()).get(
		f"/results/summary/{job}").status_code == 404
	assert client_for(make_user(role="admin")).get(
		f"/results/summary/{job}").status_code == 200


def test_job_summary_names_devices_for_an_admin_as_results_does(make_user, make_device,
                                                                client_for, session_scope):
	"""An admin's completion card for another user's job names its device by
	that user's inventory label - as Results does - not the bare address."""
	admin, other = make_user(role="admin"), make_user()
	make_device(other, ip="10.8.8.8", port=2001, label="their-edge")
	job = uuid.uuid4()
	add_result(session_scope, other, job, ip="10.8.8.8", port=2001, status="failed")
	with session_scope() as s:
		row = s.query(DeviceResult).filter_by(job_id=job).one()
		row.action_needed = "check the device"
	body = client_for(admin).get(f"/results/summary/{job}").json
	assert body["action_needed"] == [{"device": "their-edge", "text": "check the device"}]


def test_dashboard_marks_recent_jobs_that_need_a_person(operator, client_for,
                                                         session_scope):
	"""The dashboard links a recent job with an action needed to its results."""
	job = uuid.uuid4()
	now = dt.datetime.now()
	with session_scope() as s:
		s.add(DeviceResult(user_id=operator.user.id, job_id=job, started_at=now,
		                   completed_at=now, device_ip="10.0.0.1", device_port=22,
		                   device_type="cisco_ios", commands_sent=1,
		                   status="success", action_needed="save it"))
	html = client_for(operator.user).get("/dashboard").get_data(as_text=True)
	assert f'href="/results?job={job}"' in html


def test_log_download_is_owner_only(operator, client_for, session_scope,
                                    make_user):
	"""The rollout log downloads for its owner; another user gets 404."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job)
	os.makedirs(runtime.logs_dir(), exist_ok=True)
	path = os.path.join(runtime.logs_dir(), f"rollout_20260101_000000_{job}.log")
	with open(path, "w", encoding="utf-8") as f:
		f.write("log body")
	resp = client_for(operator.user).get(f"/results/download_log/{job}")
	assert resp.status_code == 200 and resp.data == b"log body"
	assert client_for(make_user()).get(
		f"/results/download_log/{job}").status_code == 404


def _register_job_meta(app, user, job_id):
	"""Register a job as active for the user in Redis, as the orchestrator does."""
	client = app.backend.redis.client
	client.hset(f"job:{job_id}:meta", mapping={
		"user_id": str(user.id), "status": "active", "device_count": 1,
		"created_at": dt.datetime.now().isoformat()})
	client.sadd(f"user_jobs:{user.id}", str(job_id))


def test_dashboard_renders(operator, client_for):
	"""The dashboard renders for an operator."""
	assert client_for(operator.user).get("/dashboard").status_code == 200


def test_dashboard_active_rollout_card_names_its_job(app, operator, client_for,
                                                    monkeypatch):
	"""The dashboard's active rollout card shows the job id's first 8
	characters, its Cancel form posts that job_id, and the elapsed timer's
	start is a JSON string."""
	job = FakeRunningJob(operator.user.id)
	job.started_at = dt.datetime(2026, 10, 8, 9, 30, 0)
	job.get_device_count = lambda: 2
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	_register_job_meta(app, operator.user, job.job_id)
	html = client_for(operator.user).get("/dashboard").get_data(as_text=True)
	assert f'<input type="hidden" name="job_id" value="{job.job_id}">' in html
	assert f'{str(job.job_id)[:8]}…</div>' in html
	assert 'const jobStart = new Date("2026-10-08T09:30:00");' in html


def test_active_jobs_lists_running_job(app, operator, client_for, monkeypatch):
	"""Active Jobs lists a running job of the user."""
	job = FakeRunningJob(operator.user.id)
	job.started_at = dt.datetime.now()
	job.get_device_count = lambda: 2
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	_register_job_meta(app, operator.user, job.job_id)
	resp = client_for(operator.user).get("/active_jobs")
	assert resp.status_code == 200 and str(job.job_id) in resp.get_data(as_text=True)


def test_the_jobs_screens_say_a_queued_job_is_queued(app, operator, client_for,
                                                     monkeypatch):
	"""A queued job reads "Queued — waiting for a free slot" on Active Jobs and on
	the dashboard's rollout card (no elapsed timer), not a bare "pending"."""
	job = FakeRunningJob(operator.user.id)
	job.started_at = None
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	_register_job_meta(app, operator.user, job.job_id)
	app.backend.redis.client.hset(f"job:{job.job_id}:meta", "status", "pending")
	client = client_for(operator.user)
	jobs_page = client.get("/active_jobs").get_data(as_text=True)
	assert ('<span class="status-pill pending queued">Queued — waiting for a free slot</span>'
	        in jobs_page)
	assert '<span class="status-pill pending">pending</span>' not in jobs_page
	dashboard = client.get("/dashboard").get_data(as_text=True)
	assert "QUEUED ROLLOUT" in dashboard and "Queued — waiting for a free slot" in dashboard
	assert f'<input type="hidden" name="job_id" value="{job.job_id}">' in dashboard
	assert "const jobStart" not in dashboard


def test_active_jobs_rollback_waits_for_the_job_to_finish(
		app, operator, client_for, monkeypatch):
	"""A running job's Rollback is disabled, inside a wrapper whose tooltip
	says it's available when the rollout finishes."""
	job = FakeRunningJob(operator.user.id)
	job.started_at = dt.datetime.now()
	job.get_device_count = lambda: 1
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	_register_job_meta(app, operator.user, job.job_id)
	html = client_for(operator.user).get("/active_jobs").get_data(as_text=True)
	wrapper = re.search(
		r'<span class="rollback-wait"[^>]*title="([^"]*)">\s*'
		r'<button type="button" class="job-action-btn btn-rollback" disabled>',
		html)
	assert wrapper is not None
	assert wrapper.group(1) == ("Available when the rollout finishes — then "
	                            "from the completion card or Results")
	assert "openRollback('" not in html


def test_active_jobs_survives_orphaned_job_meta(app, operator, client_for):
	"""Active Jobs still renders with Redis meta for a job not in memory."""
	_register_job_meta(app, operator.user, uuid.uuid4())  # not in memory
	assert client_for(operator.user).get("/active_jobs").status_code == 200


# ── Operator analytics ───────────────────────────────────────────────────────

def test_analytics_query_is_scoped_and_allowlisted(operator, client_for,
                                                   session_scope, make_user):
	"""An operator's analytics query sees only their own results (a user param
	is ignored), a field outside the allowlist gets 400, the page renders."""
	other = make_user()
	add_result(session_scope, operator.user, uuid.uuid4(), status="failed")
	add_result(session_scope, other, uuid.uuid4(), status="failed")
	rule = {"condition": "AND", "rules": [
		{"field": "status", "operator": "equal", "value": "failed"}]}
	client = client_for(operator.user)
	resp = client.post("/analytics/query", json={
		"rules": rule, "user": str(other.id)})  # non-admin can't re-scope
	assert len(resp.json["rows"]) == 1
	bad = client.post("/analytics/query", json={"rules": {
		"field": "fetched_config", "operator": "contains", "value": "x"}})
	assert bad.status_code == 400
	assert client.get("/analytics").status_code == 200


def test_analytics_names_a_failed_global_device_by_its_label(
		operator, client_for, session_scope, make_user, make_profile, make_device):
	"""The operator's Analytics names the most-failed device from the devices
	they can see - a global device by its label, as the dashboard does."""
	admin = make_user(role="admin")
	make_device(admin, ip="10.9.9.9", label="CORE-GLOBAL", is_global=True,
	            profile_id=make_profile(admin))
	add_result(session_scope, operator.user, uuid.uuid4(), ip="10.9.9.9",
	           status="failed")
	page = client_for(operator.user).get("/analytics").get_data(as_text=True)
	assert "CORE-GLOBAL" in page


def test_admin_analytics_query_with_a_user_that_is_not_a_string(client_for,
                                                                session_scope,
                                                                make_user):
	"""An admin's analytics query whose user param is another JSON type (a
	number) is ignored like a malformed id - the admin's own results - not a
	server error."""
	admin, other = make_user(role="admin"), make_user()
	add_result(session_scope, admin, uuid.uuid4(), status="failed")
	add_result(session_scope, other, uuid.uuid4(), status="failed")
	rule = {"condition": "AND", "rules": [
		{"field": "status", "operator": "equal", "value": "failed"}]}
	resp = client_for(admin).post("/analytics/query", json={"rules": rule, "user": 5})
	assert resp.status_code == 200 and len(resp.json["rows"]) == 1


def test_analytics_query_with_invalid_rules_is_400(operator, client_for):
	"""Rules the builder couldn't make (null when a rule is invalid), not an
	object, missing, or a group whose rules aren't a list: 400 with a
	message, never a 500."""
	client = client_for(operator.user)
	for body in ({"rules": None}, {"rules": []}, {"rules": "x"},
	             {"user": "me"},
	             {"rules": {"condition": "AND", "rules": None}}):
		resp = client.post("/analytics/query", json=body)
		assert resp.status_code == 400, body
		assert resp.json["status"] == "error" and resp.json["message"], body


def test_analytics_query_with_an_empty_group_returns_every_row(
		operator, client_for, session_scope):
	"""Every rule deleted (an empty root group): all the user's rows."""
	add_result(session_scope, operator.user, uuid.uuid4())
	add_result(session_scope, operator.user, uuid.uuid4(), status="failed")
	resp = client_for(operator.user).post("/analytics/query", json={
		"rules": {"condition": "AND", "rules": []}})
	assert resp.status_code == 200 and len(resp.json["rows"]) == 2


# ── Reachability blocks rollouts ─────────────────────────────────────────────

def test_rollout_to_unreachable_device_is_blocked(operator, client_for,
                                                  captured_submits,
                                                  unreachable_targets):
	"""A rollout to an unreachable device is blocked: back to /rollout/new,
	nothing submitted, the flash names the device."""
	unreachable_targets.add(("10.0.0.1", 22))
	client = client_for(operator.user)
	resp = client.post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "hostname x"})
	assert resp.headers["Location"] == "/rollout/new"
	assert captured_submits == []
	with client.session_transaction() as s:
		(_, message), = s["_flashes"]
	assert "rollout blocked" in message and "10.0.0.1:22" in message


def test_device_back_online_is_not_blocked_by_stale_cache(
		operator, client_for, captured_submits, unreachable_targets):
	"""A device cached as down but back online is re-probed at submit and the
	rollout goes through."""
	client = client_for(operator.user)
	unreachable_targets.add(("10.0.0.1", 22))
	client.post("/inventory/reachability", json={"device_ids": [str(operator.ios)]})
	unreachable_targets.clear()  # it came back; the cache still says down
	client.post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "hostname x"})
	assert len(captured_submits) == 1  # failures are re-probed at submit


def test_rollback_to_unreachable_device_is_blocked(operator, client_for,
                                                   session_scope,
                                                   captured_submits,
                                                   unreachable_targets):
	"""A rollback to an unreachable device answers 409 "rollout blocked" and
	submits nothing."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	unreachable_targets.add(("10.0.0.1", 22))
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}",
	                                      json={"commands": "no hostname"})
	assert resp.status_code == 409 and "rollout blocked" in resp.json["message"]
	assert captured_submits == []


def test_new_rollout_page_has_reachability_ui(operator, client_for):
	"""The new rollout page has the recheck button, the unreachable warning and
	a device id per row."""
	html = client_for(operator.user).get("/rollout/new").get_data(as_text=True)
	assert 'id="reachRecheck"' in html and 'id="unreachWarning"' in html
	assert f'data-device-id="{operator.ios}"' in html


# ── Tokens that can't be filled in block rollouts ─────────────────────────────

def _flashed(client):
	""":returns: the newest message flashed to this client"""
	with client.session_transaction() as s:
		return s["_flashes"][-1][1]


def test_a_launch_with_a_token_typo_is_blocked(operator, client_for, captured_submits):
	"""A token no mapping on the device has (a typo) blocks the launch before anything is
	queued - it was sent to the device literally: back to the form, the flash names the
	device and the token."""
	client = client_for(operator.user)
	resp = client.post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "hostname $$HOSTNMAE$$"})
	assert resp.headers["Location"] == "/rollout/new" and captured_submits == []
	message = _flashed(client)
	assert "rollout blocked" in message and "10.0.0.1:22" in message
	assert "$$HOSTNMAE$$: no mapping on this device" in message


def test_a_launch_with_a_token_without_a_value_is_blocked(operator, client_for, make_mapping,
                                                           captured_submits):
	"""A bound token whose value is gone (the arista device has no hostname) blocks the
	launch, naming the property."""
	make_mapping(operator.user, token="HOST", prop="hostname", devices=[operator.eos])
	client = client_for(operator.user)
	client.post("/rollout/start", data={
		"device_ids": [str(operator.eos)], "manual_commands": "hostname $$HOST$$"})
	assert captured_submits == []
	assert "$$HOST$$: no value for 'hostname'" in _flashed(client)


def test_a_launch_whose_tokens_resolve_goes_through(operator, client_for, make_mapping,
                                                     captured_submits):
	"""Tokens bound and valued on every device: the rollout is queued as before."""
	make_mapping(operator.user, token="HOST", prop="hostname", devices=[operator.ios])
	client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "hostname $$HOST$$"})
	(call,) = captured_submits
	assert call.commands == ["hostname $$HOST$$"]


def test_a_multi_platform_launch_checks_each_platforms_own_commands(
		operator, client_for, make_mapping, captured_submits):
	"""Each device is checked against its own platform's commands: a token only in the
	cisco commands (resolving there) doesn't block the arista device; one in the arista
	commands that doesn't resolve blocks the whole launch."""
	make_mapping(operator.user, token="HOST", prop="hostname", devices=[operator.ios])
	client = client_for(operator.user)
	data = {"device_ids": [str(operator.ios), str(operator.eos)]}
	client.post("/rollout/start", data={**data, "platform_commands": json.dumps(
		{"cisco_ios": "hostname $$HOST$$", "arista_eos": "hostname b"})})
	assert len(captured_submits) == 2
	captured_submits.clear()
	client.post("/rollout/start", data={**data, "platform_commands": json.dumps(
		{"cisco_ios": "hostname a", "arista_eos": "hostname $$HOST$$"})})
	assert captured_submits == []
	assert "10.0.0.2:22" in _flashed(client)


def test_a_rollback_with_a_token_that_cant_be_filled_is_refused(
		operator, client_for, session_scope, captured_submits):
	"""A rollback's commands are checked the same way: 409 naming the token, nothing
	submitted."""
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}",
	                                      json={"commands": "no ntp server $$NTP$$"})
	assert resp.status_code == 409 and captured_submits == []
	assert "$$NTP$$: no mapping on this device" in resp.json["message"]
