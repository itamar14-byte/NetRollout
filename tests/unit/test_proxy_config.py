"""src/webapp/proxy_config.py: the values the app hands nginx (site.env) and
the watcher's verdict it reads back (status.json). No nginx here — the files."""
import datetime
import json
import os

import pytest

from src.webapp import proxy_config as pc


@pytest.fixture
def home(tmp_path, monkeypatch):
	"""NETROLLOUT_HOME in a temp folder, no applied port in the environment."""
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	monkeypatch.delenv(pc.APPLIED_PORT_ENV, raising=False)
	return tmp_path


def site(home):
	return (home / "config" / "nginx" / "site.env").read_text(encoding="utf-8")


def test_writes_the_hostname_and_the_applied_port(home, monkeypatch):
	"""write_site writes site.env with exactly the hostname and the applied port
	(NETROLLOUT_HTTPS_PORT from the environment), and says it changed."""
	monkeypatch.setenv(pc.APPLIED_PORT_ENV, "8443")
	assert pc.write_site("nr01.corp.local") is True
	assert site(home) == ("NETROLLOUT_HOSTNAME=nr01.corp.local\n"
	                      "NETROLLOUT_HTTPS_PORT=8443\n")


def test_no_hostname_means_no_canonical_name(home):
	"""An empty or None hostname writes an empty NETROLLOUT_HOSTNAME, with the default
	port 443."""
	pc.write_site("")
	pc.write_site(None)
	assert site(home) == "NETROLLOUT_HOSTNAME=\nNETROLLOUT_HTTPS_PORT=443\n"


def test_the_port_is_the_published_one_not_the_setting(home, monkeypatch):
	"""applied_https_port is the published port from the environment (443 when it
	isn't a number): the System Settings port waits for `netrollout apply`, and
	redirects must keep the published one meanwhile."""
	monkeypatch.setenv(pc.APPLIED_PORT_ENV, "nonsense")
	assert pc.applied_https_port() == 443
	monkeypatch.setenv(pc.APPLIED_PORT_ENV, "9443")
	assert pc.applied_https_port() == 9443


def test_unchanged_values_are_not_rewritten(home):
	"""Writing the same hostname again returns False and leaves the file untouched
	(its mtime kept); another hostname is written."""
	assert pc.write_site("nr01") is True
	path = home / "config" / "nginx" / "site.env"
	before = path.stat().st_mtime_ns
	os.utime(path, ns=(before - 10**9, before - 10**9))   # visibly older
	assert pc.write_site("nr01") is False
	assert path.stat().st_mtime_ns == before - 10**9      # untouched
	assert pc.write_site("nr02") is True


def test_the_file_is_replaced_whole(home):
	"""After two writes the folder holds only site.env (no temp file left), with the
	latest hostname."""
	pc.write_site("nr01")
	pc.write_site("nr02")
	folder = home / "config" / "nginx"
	assert [p.name for p in folder.iterdir()] == ["site.env"]   # no temp left
	assert "nr02" in site(home)


@pytest.mark.parametrize("bad", ["nr 01", "nr01;", "https://nr01", "nr01:8443",
                                  "nr01/x", "a" * 300])
def test_invalid_hostnames_are_never_written(home, bad):
	"""A hostname that isn't one (spaces, ';', a URL, a port, a path, 300 characters)
	raises ValueError and no site.env is written."""
	with pytest.raises(ValueError):
		pc.write_site(bad)
	assert not (home / "config" / "nginx" / "site.env").exists()


def test_status_not_managed_valid_and_unreadable(home):
	"""read_status: no status.json is None (no nginx reports); a valid one gives its
	state; broken JSON or a non-object gives state "unknown"."""
	assert pc.read_status() is None                       # no nginx reports
	folder = home / "config" / "nginx"
	folder.mkdir(parents=True)
	(folder / "status.json").write_text(json.dumps(
		{"state": "applied", "message": "hostname=nr01", "time": "2026-10-04T10:00:00Z"}))
	assert pc.read_status()["state"] == "applied"
	(folder / "status.json").write_text("{half")
	assert pc.read_status()["state"] == "unknown"
	(folder / "status.json").write_text("[1, 2]")
	assert pc.read_status()["state"] == "unknown"


def test_wait_for_a_verdict_newer_than_the_write(home):
	"""wait_for_status ignores a verdict older than the write (None after the
	timeout) and returns one at least as new (here "rejected")."""
	folder = home / "config" / "nginx"
	folder.mkdir(parents=True)
	now = datetime.datetime.now(datetime.timezone.utc)
	stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
	(folder / "status.json").write_text(json.dumps(
		{"state": "applied", "time": stamp(now - datetime.timedelta(minutes=5))}))
	# only an older verdict: none comes
	assert pc.wait_for_status(now.timestamp(), timeout=0.6, poll=0.2) is None
	(folder / "status.json").write_text(json.dumps(
		{"state": "rejected", "message": "nginx: [emerg] …", "time": stamp(now)}))
	assert pc.wait_for_status(now.timestamp(), timeout=0.6)["state"] == "rejected"


def test_start_never_fails_because_of_nginx(home, capsys):
	"""sync_at_start with an invalid saved hostname raises nothing and prints that the
	nginx site values were not written."""
	class Settings:
		def get(self, key):
			return "not a hostname!"
	pc.sync_at_start(Settings())                          # no exception
	assert "nginx site values not written" in capsys.readouterr().out


def test_waiting_for_a_hostname_ignores_other_reloads(home):
	"""Waiting for a hostname, an "applied" verdict for another hostname is not the
	answer (a reissued certificate alone reloads nginx a moment before the new
	hostname does); one naming it is, a rejection always counts, and "" waits
	for "hostname=(none)"."""
	folder = home / "config" / "nginx"
	folder.mkdir(parents=True)
	now = datetime.datetime.now(datetime.timezone.utc)
	t = now.strftime("%Y-%m-%dT%H:%M:%SZ")
	def status(state, message):
		(folder / "status.json").write_text(json.dumps(
			{"state": state, "message": message, "time": t}))
	status("applied", "hostname=old.lab https_port=443 app=app:8080")
	assert pc.wait_for_status(now.timestamp(), timeout=0.6, poll=0.2,
	                          hostname="new.lab") is None
	status("applied", "hostname=new.lab https_port=443 app=app:8080")
	assert pc.wait_for_status(now.timestamp(), timeout=0.6, hostname="new.lab")["state"] == "applied"
	status("rejected", "nginx: [emerg] …")                  # a rejection always counts
	assert pc.wait_for_status(now.timestamp(), timeout=0.6, hostname="new.lab")["state"] == "rejected"
	status("applied", "hostname=(none) https_port=443 app=app:8080")   # cleared
	assert pc.wait_for_status(now.timestamp(), timeout=0.6, hostname="")["state"] == "applied"


# ── Stage 9.1: the installer's hostname, the server's IPs ──

def test_the_installers_hostname_seeds_the_setting(home, monkeypatch):
	"""seed_hostname_from_site: without site.env the seed stays empty; with it, the
	seed becomes site.env's hostname; a seed already set is kept."""
	env_name = pc.HOSTNAME_SEED_ENV
	# setenv (not delenv): monkeypatch then removes what the code sets
	monkeypatch.setenv(env_name, "")
	pc.seed_hostname_from_site()                  # no site.env: nothing
	assert os.environ[env_name] == ""
	pc.write_site("nr01.corp.local")              # as the installer will
	pc.seed_hostname_from_site()
	assert os.environ[env_name] == "nr01.corp.local"
	monkeypatch.setenv(env_name, "explicit.lab")  # a non-Docker run names one
	pc.seed_hostname_from_site()
	assert os.environ[env_name] == "explicit.lab"


@pytest.mark.parametrize("value, expected", [
	("", []), ("10.1.1.5", ["10.1.1.5"]),
	("10.1.1.5, 192.168.1.2 fe80::1", ["10.1.1.5", "192.168.1.2", "fe80::1"]),
	("10.1.1.5,not-an-ip,10.1.1.5", ["10.1.1.5"])])
def test_server_ips(monkeypatch, value, expected):
	"""NETROLLOUT_SERVER_IPS is split on commas and spaces into IPs, keeping order,
	dropping what isn't an IP and duplicates."""
	monkeypatch.setenv(pc.SERVER_IPS_ENV, value)
	assert pc.server_ips() == expected
