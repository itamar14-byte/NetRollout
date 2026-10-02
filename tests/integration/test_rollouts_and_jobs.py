"""Rollout routes (captured, never executed), cancel, SSE stream, rollback,
results / Verify Diff / log download, dashboards, operator analytics."""
import datetime as dt
import os
import threading
import uuid
from types import SimpleNamespace

import pytest

from src import runtime
from src.db.settings import SETTINGS
from src.db.tables import DeviceResult, JobMetadata

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

CONFIG_SNAPSHOT_RETENTION_DAYS = SETTINGS["config_snapshot_retention_days"].default


@pytest.fixture
def operator(make_user, make_profile, make_device):
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
	resp = client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "hostname $$H$$\n\n",
		"_verify": "on", "comment": "chg-1"})
	assert resp.status_code == 302 and "/active_jobs?new=" in resp.headers["Location"]
	(call,) = captured_submits
	assert call.commands == ["hostname $$H$$"]
	assert call.params.verify is True and call.comment == "chg-1"
	assert [d.ip for d in call.devices] == ["10.0.0.1"]


def test_multi_platform_rollout_submits_one_job_per_platform(
		operator, client_for, captured_submits):
	import json
	client_for(operator.user).post("/rollout/start", data={
		"device_ids": [str(operator.ios), str(operator.eos)],
		"platform_commands": json.dumps({"cisco_ios": "hostname a",
		                                 "arista_eos": "hostname b"})})
	by_platform = {c.devices[0].device_type: c.commands for c in captured_submits}
	assert by_platform == {"cisco_ios": ["hostname a"],
	                       "arista_eos": ["hostname b"]}


@pytest.mark.parametrize("form_fn", [
	lambda o: {"manual_commands": "x"},                                # no devices
	lambda o: {"device_ids": [str(o.ios)]},                            # no commands
	lambda o: {"device_ids": [str(o.bare)], "manual_commands": "x"},   # no profile
	lambda o: {"device_ids": ["not-a-uuid"], "manual_commands": "x"},
])
def test_invalid_rollouts_are_refused(operator, client_for, captured_submits,
                                      form_fn):
	resp = client_for(operator.user).post("/rollout/start",
	                                      data=form_fn(operator))
	assert resp.headers["Location"] == "/rollout/new"
	assert captured_submits == []


def test_same_target_selected_twice_is_refused(operator, client_for,
                                               make_user, make_profile,
                                               make_device, captured_submits):
	# the operator's own entry and a global entry for the same box
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
	# port-forwarded lab nodes: same host IP, different SSH ports
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
	resp = client_for(make_user()).post("/rollout/start", data={
		"device_ids": [str(operator.ios)], "manual_commands": "x"})
	assert resp.headers["Location"] == "/rollout/new"
	assert captured_submits == []


# ── Cancel, stream, rollback ─────────────────────────────────────────────────

class FakeRunningJob:
	def __init__(self, user_id, messages=()):
		self.job_id = uuid.uuid4()
		self.user_id = user_id
		self.cancelled = threading.Event()
		self._messages = list(messages)

	def cancel(self):
		self.cancelled.set()

	def is_alive(self):
		return True

	def get_log_history(self):
		return ["history-line"]

	def get_log_queue(self):
		msgs = [{"type": "message", "data": m.encode()} for m in self._messages]
		return SimpleNamespace(get_message=lambda timeout: msgs.pop(0) if msgs
		                       else None, close=lambda: None)


def test_owner_cancels_running_job(app, operator, client_for, monkeypatch):
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	resp = client_for(operator.user).post("/rollout/cancel",
	                                      data={"job_id": str(job.job_id)})
	assert resp.json["status"] == "ok" and job.cancelled.is_set()
	status = app.backend.redis.client.hget(f"job:{job.job_id}:meta", "status")
	assert status == b"cancelling"


def test_other_user_cannot_cancel(app, operator, client_for, make_user,
                                  monkeypatch):
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	resp = client_for(make_user()).post("/rollout/cancel",
	                                    data={"job_id": str(job.job_id)})
	assert resp.status_code == 403 and not job.cancelled.is_set()


def test_stream_replays_history_then_tails_live(app, operator, client_for,
                                                monkeypatch):
	job = FakeRunningJob(operator.user.id, messages=["live-line", "__done__"])
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	body = client_for(operator.user).get(
		f"/rollout/stream/{job.job_id}").get_data(as_text=True)
	assert body.index("data: history-line") < body.index("data: live-line")
	assert body.rstrip().endswith("event: done\ndata:")


def test_stream_of_another_users_job_forbidden(app, operator, client_for,
                                               make_user, monkeypatch):
	job = FakeRunningJob(operator.user.id)
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	resp = client_for(make_user()).get(f"/rollout/stream/{job.job_id}")
	assert resp.status_code == 403


def test_rollback_targets_successful_devices_once_per_ip(
		operator, client_for, session_scope, make_user, make_profile,
		make_device, captured_submits):
	# a global device sharing an IP with the operator's own device
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


def test_rollback_matches_on_ip_and_port(operator, client_for, session_scope,
                                         make_profile, make_device,
                                         captured_submits):
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


# ── Results / Verify Diff / logs ─────────────────────────────────────────────

def test_results_distinguish_devices_sharing_an_ip(operator, client_for,
                                                   session_scope, make_profile,
                                                   make_device):
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

def test_results_show_verify_diff_and_expired_states(operator, client_for,
                                                     session_scope):
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
	assert other.status_code == 403


def test_config_diff_returns_the_engines_verdicts(operator, client_for,
                                                  session_scope):
	# The page shows the server's matcher (sections, removals, variables),
	# not a text search of its own
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
	# Visible without reading the log: a badge on the job, the instruction
	# per device in the expanded job, a marker on the device row
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


def test_job_summary_for_the_completion_card(operator, client_for,
                                            session_scope, make_user):
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


def test_dashboard_marks_recent_jobs_that_need_a_person(operator, client_for,
                                                         session_scope):
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
	client = app.backend.redis.client
	client.hset(f"job:{job_id}:meta", mapping={
		"user_id": str(user.id), "status": "active", "device_count": 1,
		"created_at": dt.datetime.now().isoformat()})
	client.sadd(f"user_jobs:{user.id}", str(job_id))


def test_dashboard_renders(operator, client_for):
	assert client_for(operator.user).get("/dashboard").status_code == 200


def test_active_jobs_lists_running_job(app, operator, client_for, monkeypatch):
	job = FakeRunningJob(operator.user.id)
	job.started_at = dt.datetime.now()
	job.get_device_count = lambda: 2
	monkeypatch.setitem(app.orchestrator._jobs, job.job_id, job)
	_register_job_meta(app, operator.user, job.job_id)
	resp = client_for(operator.user).get("/active_jobs")
	assert resp.status_code == 200 and str(job.job_id) in resp.get_data(as_text=True)


def test_active_jobs_survives_orphaned_job_meta(app, operator, client_for):
	_register_job_meta(app, operator.user, uuid.uuid4())  # not in memory
	assert client_for(operator.user).get("/active_jobs").status_code == 200


# ── Operator analytics ───────────────────────────────────────────────────────

def test_analytics_query_is_scoped_and_allowlisted(operator, client_for,
                                                   session_scope, make_user):
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


# ── Reachability blocks rollouts ─────────────────────────────────────────────

def test_rollout_to_unreachable_device_is_blocked(operator, client_for,
                                                  captured_submits,
                                                  unreachable_targets):
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
	job = uuid.uuid4()
	add_result(session_scope, operator.user, job, ip="10.0.0.1")
	unreachable_targets.add(("10.0.0.1", 22))
	resp = client_for(operator.user).post(f"/rollout/rollback/{job}",
	                                      json={"commands": "no hostname"})
	assert resp.status_code == 409 and "rollout blocked" in resp.json["message"]
	assert captured_submits == []


def test_new_rollout_page_has_reachability_ui(operator, client_for):
	html = client_for(operator.user).get("/rollout/new").get_data(as_text=True)
	assert 'id="reachRecheck"' in html and 'id="unreachWarning"' in html
	assert f'data-device-id="{operator.ios}"' in html
