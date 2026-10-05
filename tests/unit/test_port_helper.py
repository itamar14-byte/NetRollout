"""The port helper's decisions and records (src/setup/port.py), on an
install in a scratch folder - and that the app's side of the contract
(src/webapp/port_apply.py) reads them as meant."""
import json

import pytest

from src import site_env
from src.setup import manage, port
from src.webapp import port_apply
from tests.unit.test_setup import home, installed, run  # noqa: F401 - fixture

NOW = 1_800_000_000.0


def request(port_, id_="r1"):
	site_env.update({site_env.PORT_REQUEST: str(port_), site_env.PORT_REQUEST_ID: id_,
	                 site_env.PORT_REQUESTED_AT: str(int(NOW)), site_env.PORT_CONFIRMED: None})


def env():
	return manage.env_read()


@pytest.fixture
def install(home):
	installed(home, "--https-port", "8443")
	return home


def test_the_app_and_the_helper_name_the_same_status_file():
	assert port.STATUS_FILE == port_apply.STATUS_FILE


def test_nothing_to_do_without_a_request(install):
	assert port.next_step({}, NOW).action == "none"
	assert not port.status_path().exists()


def test_a_trial_confirmed_from_the_new_port_is_kept(install):
	request(9443)
	step = port.next_step({}, NOW)
	assert (step.action, step.port, step.id) == ("try", 9443, "r1")

	port.open_trial(9443)
	assert 'ports:\n      - "9443:443"' in port.trial_path().read_text()
	assert env()["COMPOSE_FILE"].split(",")[-1] == port.TRIAL_ENTRY
	port.trying(9443, "r1", now=NOW)
	assert port_apply.state(9443)["state"] == "trying"          # the page offers the link
	assert port.next_step({}, NOW + 30).action == "wait"

	assert port_apply.confirm("r1", 9443) is None               # the browser on the new port
	assert port.next_step({}, NOW + 31).action == "keep"
	port.close("keep", "r1", now=NOW + 32)
	assert env()["HTTPS_PORT"] == "9443"
	assert port.TRIAL_ENTRY not in env()["COMPOSE_FILE"]
	assert not port.trial_path().exists()
	assert site_env.read()[site_env.HTTPS_PORT] == "9443"     # nginx's redirects follow
	assert port_apply.serving_port() == 9443
	assert port_apply.state(9443)["state"] == "applied"
	assert port.next_step({}, NOW + 40).action == "none"        # handled once


def test_an_unconfirmed_trial_rolls_back_and_isnt_retried(install):
	request(9443)
	port.open_trial(9443)
	port.trying(9443, "r1", now=NOW)
	step = port.next_step({}, NOW + port.TRIAL_SECONDS + 1)
	assert step.action == "rollback" and "not confirmed within 120 s" in step.message
	port.close("rollback", "r1", step.message, now=NOW + 125)
	assert env()["HTTPS_PORT"] == "8443" and port.TRIAL_ENTRY not in env()["COMPOSE_FILE"]
	shown = port_apply.state(9443)
	assert shown["state"] == "rolled_back" and "a firewall?" in shown["message"]
	assert port.next_step({}, NOW + 200).action == "none"


def test_a_new_request_replaces_a_running_trial(install):
	request(9443, "r1")
	port.open_trial(9443)
	port.trying(9443, "r1", now=NOW)
	request(9555, "r2")
	step = port.next_step({}, NOW + 10)
	assert (step.action, step.id, step.message) == ("rollback", "r1", "replaced by a newer request")
	port.close("rollback", "r1", step.message, now=NOW + 11)
	assert port.next_step({}, NOW + 12).action == "try"


@pytest.mark.parametrize("wanted, busy, says", [
	(9443, {9443: "IIS"}, "port 9443 is in use on this computer (by IIS)"),
	(80, {}, "'80' isn't a usable HTTPS port"),
	(70000, {}, "'70000' isn't a usable HTTPS port"),
])
def test_a_request_that_cant_be_tried_fails_with_why(install, wanted, busy, says):
	request(wanted)
	step = port.next_step(busy, NOW)
	assert step.action == "none" and step.message == says
	recorded = json.loads(port.status_path().read_text())
	assert (recorded["state"], recorded["id"], recorded["port"], recorded["message"]) == \
		("failed", "r1", 8443, says)
	assert env()["HTTPS_PORT"] == "8443"


def test_the_port_in_use_already_is_applied_at_once(install):
	request(8443)
	assert port.next_step({}, NOW).action == "none"
	assert json.loads(port.status_path().read_text())["state"] == "applied"


def test_a_trial_whose_helper_died_rolls_back_later(install):
	# opened, nginx up, then nothing ran until long after the deadline
	request(9443)
	port.open_trial(9443)
	port.trying(9443, "r1", now=NOW)
	assert port.next_step({}, NOW + 3600).action == "rollback"


def test_the_cli_prints_one_line_to_act_on(install):
	request(9443)
	assert run(["port-next"]) == (0, ["try 9443 r1"])
	assert run(["port-open", "--port", "9443"])[0] == 0
	assert run(["port-trying", "--port", "9443", "--id", "r1"])[0] == 0
	assert run(["port-next"]) == (0, ["wait 9443 r1"])
	assert run(["port-close", "--outcome", "rollback", "--id", "r1", "--message", "test"])[0] == 0
	assert run(["port-next"]) == (0, ["none - -"])
	request(9443, "r2")
	code, out = run(["port-next", "--busy-ports", "9443=IIS"])
	assert out == ["none 9443 r2", "port 9443 is in use on this computer (by IIS)"]


def test_the_scripts_stopwatch_ends_a_trial_whatever_the_clocks_say(install):
	# Docker Desktop's VM clock can lag Windows' by minutes: the script times
	# the trial itself and closes it; the recorded deadline isn't consulted
	request(9443)
	port.open_trial(9443)
	port.trying(9443, "r1", now=NOW)
	assert run(["port-close", "--outcome", "rollback", "--id", "r1", "--timed-out"])[0] == 0
	recorded = json.loads(port.status_path().read_text())
	assert (recorded["state"], recorded["message"]) == ("rolled_back", port.timed_out(9443))
	assert "not confirmed within 120 s - port 9443" in recorded["message"]
	assert port.TRIAL_ENTRY not in env()["COMPOSE_FILE"] and not port.trial_path().exists()
