"""src/access/port.py: the app's side of the port helper contract.
No helper here — the tests write its status file the way the contract says."""
import json
import time

import pytest

from src.access import site_env, port as pa, nginx as proxy_config


@pytest.fixture
def home(tmp_path, monkeypatch):
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	monkeypatch.delenv(pa.PUBLISHED_PORT_ENV, raising=False)
	(tmp_path / "config").mkdir()
	return tmp_path / "config"


def helper(config, **status):
	(config / pa.STATUS_FILE).write_text(json.dumps(status))


def request_id():
	return pa.read_request()["id"]


# ── the serving port ──

def test_serving_port_without_a_helper_is_the_published_one(home, monkeypatch):
	"""Without a helper status, the serving port is the published one: 443 by
	default, else NETROLLOUT_HTTPS_PORT."""
	assert pa.serving_port() == 443
	monkeypatch.setenv(pa.PUBLISHED_PORT_ENV, "8443")
	assert pa.serving_port() == 8443


def test_the_helper_knows_better_than_the_containers_env(home, monkeypatch):
	"""The helper's applied port wins over the published one in the app's env (the
	helper recreates nginx only: the app's env keeps the old port)."""
	monkeypatch.setenv(pa.PUBLISHED_PORT_ENV, "443")
	helper(home, state="applied", port=9443, id="a1")
	assert pa.serving_port() == 9443


def test_a_trial_counts_only_once_confirmed(home):
	"""During a trial the serving port stays the old one until site.env's
	confirmation names this trial's id; then it is the trial port."""
	helper(home, state="trying", port=443, trying=8443, id="a1")
	assert pa.serving_port() == 443
	site_env.update({site_env.PORT_CONFIRMED: "other"})
	assert pa.serving_port() == 443
	site_env.update({site_env.PORT_CONFIRMED: "a1"})
	assert pa.serving_port() == 8443


def test_a_broken_status_falls_back_to_the_published_port(home, monkeypatch):
	"""A status file that isn't JSON, or whose port isn't a number, falls back to the
	published port."""
	monkeypatch.setenv(pa.PUBLISHED_PORT_ENV, "8443")
	(home / pa.STATUS_FILE).write_text("{half")
	assert pa.serving_port() == 8443
	helper(home, state="applied", port="not a port")
	assert pa.serving_port() == 8443


# ── requests ──

def test_a_request_carries_the_port_and_a_new_id_each_time(home):
	"""A request writes the port and a 16-character id; the same port requested again
	(Try again) gets a new id; no temp file is left beside site.env."""
	pa.request_port(8443)
	first = pa.read_request()
	assert first["port"] == 8443 and len(first["id"]) == 16
	pa.request_port(8443)                      # "Try again": same port, new id
	assert pa.read_request()["id"] != first["id"]
	assert [p.name for p in site_env.folder().iterdir()] == [site_env.FILE]   # no temp left


def test_undo_puts_the_previous_request_back(home):
	"""The undo a request returns puts back what was there before: no request after
	the first, the earlier site.env values after a second."""
	undo = pa.request_port(8443)
	undo()
	assert pa.read_request() is None
	pa.request_port(8443)
	before = site_env.read()
	undo = pa.request_port(9443)
	undo()
	assert site_env.read() == before


@pytest.mark.parametrize("bad", [0, 70000, "443; rm", ""])
def test_an_invalid_port_is_never_requested(home, bad):
	"""An invalid port (0, 70000, text, empty) raises ValueError and writes nothing."""
	with pytest.raises(ValueError):
		pa.request_port(bad)
	assert pa.read_request() is None and not site_env.path().exists()


# ── what the page shows ──

def test_nothing_pending(home):
	"""With nothing requested, the page state is applied, saved and serving both the
	port."""
	assert pa.state(443) == {"saved": 443, "serving": 443, "state": "applied"}


def test_no_helper_means_run_apply(home):
	"""A request with no helper status gives state "manual" (run netrollout apply)."""
	pa.request_port(8443)
	assert pa.state(8443)["state"] == "manual"


def test_waiting_for_the_helper_is_bounded(home):
	"""With a helper present, a request is "waiting"; once older than WAIT_SECONDS it
	becomes "no_helper_answer"."""
	helper(home, state="applied", port=443, id="")
	pa.request_port(8443)
	assert pa.state(8443)["state"] == "waiting"
	old = time.time() - pa.WAIT_SECONDS - 5
	site_env.update({site_env.PORT_REQUESTED_AT: str(int(old))})
	assert pa.state(8443)["state"] == "no_helper_answer"


def test_a_trial_then_its_confirmation(home):
	"""The helper's trial for this request shows as "trying" with the port and id;
	confirming it succeeds and shows "confirming"; the helper's applied status
	then gives applied on the new port."""
	pa.request_port(8443)
	rid = request_id()
	helper(home, state="trying", port=443, trying=8443, id=rid, deadline=time.time() + 120)
	s = pa.state(8443)
	assert s["state"] == "trying" and s["trying"] == 8443 and s["id"] == rid
	assert pa.confirm(rid, 8443) is None
	# confirmed: not offered again while the helper drops the old port
	assert pa.state(8443)["state"] == "confirming"
	helper(home, state="applied", port=8443, id=rid)
	assert pa.state(8443) == {"saved": 8443, "serving": 8443, "state": "applied"}


def test_a_rollback_shows_the_helpers_reason(home):
	"""A rollback for this request shows "rolled_back" with the helper's message,
	still serving the old port; a new request is "waiting" again."""
	pa.request_port(8443)
	rid = request_id()
	helper(home, state="rolled_back", port=443, id=rid, message="not confirmed within 120 s")
	s = pa.state(8443)
	assert s["state"] == "rolled_back" and s["message"] == "not confirmed within 120 s"
	assert s["serving"] == 443
	pa.request_port(8443)                      # Try again: a new request
	assert pa.state(8443)["state"] == "waiting"


def test_another_requests_answer_does_not_count(home):
	"""A helper answer for another request id is ignored: still "waiting"."""
	pa.request_port(8443)
	helper(home, state="failed", port=443, id="an-older-request", message="port in use")
	assert pa.state(8443)["state"] == "waiting"


# ── confirming ──

def test_confirm_refuses_what_is_not_a_live_trial(home):
	"""confirm refuses with a message: no trial running, another id, the page reached
	through the old port, or past the deadline ("Too late"); nothing is confirmed."""
	pa.request_port(8443)
	rid = request_id()
	assert "no port change waiting" in pa.confirm(rid, 8443)
	helper(home, state="trying", port=443, trying=8443, id=rid, deadline=time.time() + 120)
	assert "no port change waiting" in pa.confirm("other", 8443)
	assert "this page came through port 443" in pa.confirm(rid, 443)
	helper(home, state="trying", port=443, trying=8443, id=rid, deadline=time.time() - 1)
	assert "Too late" in pa.confirm(rid, 8443)
	assert site_env.PORT_CONFIRMED not in site_env.read()


def test_a_failed_write_names_the_request_file(home):
	"""When site.env can't be replaced, the OSError names site.env, and no temp file
	is left."""
	site_env.path().mkdir(parents=True)             # can't be replaced
	with pytest.raises(OSError) as e:
		pa.request_port(8443)
	assert e.value.filename == str(site_env.path())
	assert [p.name for p in site_env.folder().iterdir()] == [site_env.FILE]   # no temp left


# ── one file for nginx and the helper (the merge) ──

def test_the_hostname_and_a_port_request_share_site_env(home):
	"""A hostname save after a port request keeps the request in site.env; the
	hostname is the new one and the port in use stays 443."""
	proxy_config.write_site("nr01.lab")
	pa.request_port(8443)
	proxy_config.write_site("nr02.lab")              # a later hostname save
	values = site_env.read()
	assert values[site_env.HOSTNAME] == "nr02.lab"
	assert values[site_env.HTTPS_PORT] == "443"      # in use, not the request
	assert pa.read_request()["port"] == 8443         # the request survived


def test_a_new_request_clears_an_old_confirmation(home):
	"""A new request removes a confirmation of the previous one: the helper must
	never see a confirmation that isn't for this request."""
	pa.request_port(8443)
	site_env.update({site_env.PORT_CONFIRMED: pa.read_request()["id"]})
	pa.request_port(9443)
	assert site_env.PORT_CONFIRMED not in site_env.read()
