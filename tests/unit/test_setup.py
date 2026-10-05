"""src/setup: the install questions, their checks and defaults, what an
install writes, and the contract with the host scripts (exit codes)."""
import datetime

import pytest

from src import certs, runtime, site_env
from src.setup import __main__ as cli
from src.setup import answers as A
from src.setup import files


@pytest.fixture
def home(tmp_path, monkeypatch):
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	return tmp_path


def scripted(*replies):
	"""An input() that answers in order, and records what was asked."""
	asked, replies = [], list(replies)

	def read(prompt):
		asked.append(prompt)
		if not replies:
			raise EOFError
		return replies.pop(0)
	read.asked = asked
	return read


def run(argv, *replies):
	out = []
	code = cli.main(argv, read=scripted(*replies), write=out.append)
	return code, out


WINDOWS = ["--os", "windows", "--computer-name", "NR-SRV01",
           "--host-timezone", "Israel Standard Time",
           "--server-ips", "10.0.0.5,192.168.1.20", "--account", "CORP\\admin"]


# ── checks ──

@pytest.mark.parametrize("value, expected", [
	("NR-SRV01", "nr-srv01"), ("netrollout.corp.local.", "netrollout.corp.local"),
	("10.0.0.5", "10.0.0.5")])
def test_hostnames(value, expected):
	assert A.check_hostname(value) == expected


@pytest.mark.parametrize("bad", ["", "https://nr01", "nr 01", "nr01:8443"])
def test_bad_hostnames(bad):
	with pytest.raises(A.Invalid):
		A.check_hostname(bad)


def test_a_busy_port_names_who_and_suggests_a_free_one():
	busy = {443: "Windows' HTTP service", 8443: ""}
	with pytest.raises(A.Invalid) as e:
		A.check_port("443", busy)
	assert "in use on this computer (by Windows' HTTP service)" in str(e.value)
	assert "e.g. 9443" in str(e.value)
	assert A.check_port("9443", busy) == 9443
	for bad in ("80", "0", "70000", "https"):
		with pytest.raises(A.Invalid):
			A.check_port(bad, {})


@pytest.mark.parametrize("value, expected", [
	("Israel Standard Time", "Asia/Jerusalem"),   # Windows -> IANA (CLDR)
	("W. Europe Standard Time", "Europe/Berlin"),
	("Asia/Jerusalem", "Asia/Jerusalem"), ("UTC", "UTC")])
def test_timezones(value, expected):
	assert A.check_timezone(value) == expected


def test_an_unknown_timezone():
	with pytest.raises(A.Invalid):
		A.check_timezone("Mars/Olympus")


def test_defaults_come_from_the_host():
	facts = A.Facts(computer_name="NR-SRV01", timezone="Israel Standard Time",
	                busy_ports={443: ""})
	answers = A.collect(facts, {}, interactive=False)
	assert answers == A.Answers("nr-srv01", 8443, True, False, "Asia/Jerusalem")
	# a computer name that isn't a hostname, an unknown timezone
	plain = A.collect(A.Facts(computer_name="My PC!", timezone="?"), {}, False)
	assert (plain.hostname, plain.timezone) == ("netrollout", "UTC")


# ── asking ──

def test_enter_accepts_the_default_and_a_wrong_answer_is_asked_again():
	read, said = scripted("", "443", "8443", "maybe", "n", "", ""), []
	answers = A.collect(A.Facts(computer_name="box", busy_ports={443: ""}), {},
	                    True, read, said.append)
	assert read.asked[0] == "Hostname people will use [box]: "
	assert read.asked[1] == "HTTPS port [8443]: "            # 443 taken
	assert answers == A.Answers("box", 8443, False, False, "UTC")
	assert any("Port 443 is in use" in s for s in said)
	assert "  Answer y or n." in said


def test_input_ending_is_not_an_answer():
	with pytest.raises(A.NoAnswer):
		A.collect(A.Facts(), {}, True, scripted(), print)


def test_given_answers_are_checked_all_at_once():
	with pytest.raises(A.Invalid) as e:
		A.collect(A.Facts(busy_ports={443: ""}),
		          {"hostname": "https://x", "https_port": "443"}, False)
	assert str(e.value).count("\n") == 1                  # both reported


# ── the install ──

def test_an_install_writes_everything(home):
	code, out = run(["init", "--licence-accepted", *WINDOWS, "--busy-ports", "80=IIS"],
	                "", "", "", "", "")
	assert code == 0
	assert {p.name for p in home.iterdir()} == {".env", "logs", "config",
	                                             "certs", "backups"}
	env = dict(line.split("=", 1) for line in
	           (home / ".env").read_text(encoding="utf-8").splitlines()
	           if line and not line.startswith("#"))
	assert set(env) == {"POSTGRES_PASSWORD", "NETROLLOUT_DB_PASSWORD",
	                    "GRAFANA_DB_PASSWORD", "REDIS_PASSWORD",
	                    "GRAFANA_ADMIN_PASSWORD", "SECRET_KEY",
	                    "NETROLLOUT_ENCRYPTION_KEY", "HTTPS_PORT", "TZ",
	                    "NETROLLOUT_SERVER_IPS", "COMPOSE_PROFILES",
	                    "COMPOSE_PATH_SEPARATOR", "COMPOSE_FILE"}   # 13
	assert env["TZ"] == "Asia/Jerusalem" and env["HTTPS_PORT"] == "443"
	assert env["COMPOSE_FILE"] == "compose.yaml"            # port 80 is IIS's
	assert env["COMPOSE_PROFILES"] == "monitoring"
	assert env["NETROLLOUT_SERVER_IPS"] == "10.0.0.5,192.168.1.20"
	# secrets: strong, distinct, URL-safe
	secrets = [env[k] for k in env if k.endswith(("PASSWORD", "KEY"))]
	assert len(set(secrets)) == 7 and all(len(s) >= 44 for s in secrets)
	assert all(env[k].isalnum() for k in env if k.endswith("PASSWORD"))
	header = (home / ".env").read_text(encoding="utf-8").splitlines()[0]
	assert "written by the installer" in header and "CORP\\admin" in header
	assert site_env.read() == {site_env.HOSTNAME: "nr-srv01",
	                           site_env.HTTPS_PORT: "443"}
	dns, ips = certs.names_in((home / "certs" / certs.CERT_FILE).read_bytes())
	assert dns == ["nr-srv01"] and [str(i) for i in ips] == ["10.0.0.5", "192.168.1.20"]


def test_port_80_free_adds_the_redirect(home):
	assert run(["init", "--licence-accepted", "--defaults"])[0] == 0
	assert "COMPOSE_FILE=compose.yaml,compose.http.yaml" in \
	       (home / ".env").read_text(encoding="utf-8")


def test_never_twice(home):
	assert run(["init", "--licence-accepted", "--defaults"])[0] == 0
	before = (home / ".env").read_bytes()
	code, out = run(["init", "--licence-accepted", "--defaults"])
	assert code == 2 and "already installed" in out[0]
	assert (home / ".env").read_bytes() == before


def test_init_needs_the_scripts_licence_acceptance(home):
	# the notice is the scripts' (shown before Docker is installed); without
	# their word that it was accepted nothing is installed
	code, out = run(["init", *WINDOWS], "", "", "", "", "")
	assert code == 1 and "licence notice wasn't accepted" in out[-1]
	assert not (home / ".env").exists()
	assert run(["init", "--defaults"])[0] == 1


def test_unattended_with_bad_answers_writes_nothing(home):
	code, out = run(["init", "--licence-accepted", "--defaults", "--https-port", "80",
	                 "--timezone", "Mars/Olympus"])
	assert code == 1 and len(out) == 2
	assert not (home / ".env").exists()


def test_an_organisation_certificate_must_be_in_place_and_cover_the_name(home):
	argv = ["init", "--licence-accepted", "--defaults", "--hostname", "nr01.corp.local",
	        "--org-certificate", "y"]
	code, out = run(argv)
	assert code == 1 and "Put the certificate" in out[0]
	certs.selfsigned("other.corp.local", [], runtime.certs_dir())
	(runtime.certs_dir() / certs.SELFSIGNED_MARKER).unlink()
	code, out = run(argv)
	assert code == 1 and "doesn't cover nr01.corp.local" in out[0]
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	before = (runtime.certs_dir() / certs.CERT_FILE).read_bytes()
	assert run(argv)[0] == 0
	assert (runtime.certs_dir() / certs.CERT_FILE).read_bytes() == before   # kept


def test_check_writes_nothing(home):
	code, out = run(["check", "--hostname", "nr01", "--https-port", "8443"])
	assert (code, out) == (0, ["ok"])
	assert list(home.iterdir()) == []
	assert run(["check", "--https-port", "80"])[0] == 1


def test_a_write_failure_leaves_no_env(home, monkeypatch):
	def denied(*a, **k):
		raise PermissionError(13, "Permission denied", str(home / "certs"))
	monkeypatch.setattr(certs, "selfsigned", denied)
	code, out = run(["init", "--licence-accepted", "--defaults"])
	assert code == 1 and "Permission denied" in out[-1]
	assert not (home / ".env").exists()


def test_the_env_text_is_stable():
	answers = A.Answers("nr01", 8443, False, False, "UTC")
	keys = {k: "x" for k in files.generate_secrets()}
	text = files.env_text(answers, A.Facts(os="linux"), keys,
	                      datetime.datetime(2026, 10, 5, 12, 0), "1.0.0", True)
	assert text.startswith("# NetRollout 1.0.0 — written by the installer "
	                       "on 2026-10-05 12:00.\n")
	assert "Docker Engine (Apache-2.0)" in text and "COMPOSE_PROFILES=\n" in text


# ── a developer's setup ──

def test_init_dev(home):
	code, out = run(["init", "--dev"])
	assert code == 0
	env = (home / ".env").read_text(encoding="utf-8")
	assert "NETROLLOUT_VERSION=dev" in env
	assert f"COMPOSE_FILE={files.DEV_COMPOSE}" in env
	app = (home / "config" / "runtime.env").read_text(encoding="utf-8")
	db_password = next(line.split("=", 1)[1] for line in env.splitlines()
	                   if line.startswith("NETROLLOUT_DB_PASSWORD="))
	assert f"PG_PASSWORD={db_password}" in app and "PG_HOST=127.0.0.1" in app
	assert "NETROLLOUT_ENCRYPTION_KEY" not in app      # the dev key file stays
	assert certs.names_in((home / "certs" / certs.CERT_FILE).read_bytes())[0] == ["localhost"]
	assert run(["init", "--dev"])[0] == 2               # never twice


# ── prepare-start and status (src/setup/manage.py) ──

from src.setup import manage  # noqa: E402


def installed(home, *extra):
	assert run(["init", "--licence-accepted", "--defaults", "--hostname",
	            "nr01.corp.local", "--server-ips", "10.0.0.5", *extra])[0] == 0
	return manage.env_read()


def test_env_set_edits_in_place_and_only_script_keys(home):
	installed(home)
	before = (home / ".env").read_text(encoding="utf-8")
	assert manage.env_set({"TZ": "Europe/London"}) is True
	after = (home / ".env").read_text(encoding="utf-8")
	assert after == before.replace("TZ=UTC\n", "TZ=Europe/London\n")   # comments kept
	assert manage.env_set({"TZ": "Europe/London"}) is False
	with pytest.raises(ValueError):
		manage.env_set({"SECRET_KEY": "x"})


def test_port_80_taken_later_turns_the_redirect_off_and_back_on(home):
	env = installed(home)
	assert env["COMPOSE_FILE"] == "compose.yaml,compose.http.yaml"
	said = manage.prepare_start({80: "Windows' HTTP service"}, ["10.0.0.5"])
	assert manage.env_read()["COMPOSE_FILE"] == "compose.yaml"
	assert said == ["Port 80 is in use (by Windows' HTTP service) - starting without "
	                "the http -> https redirect (it comes back by itself once port 80 "
	                "is free)."]
	assert manage.prepare_start({80: "x"}, ["10.0.0.5"]) == []     # already off
	said = manage.prepare_start({}, ["10.0.0.5"])
	assert manage.env_read()["COMPOSE_FILE"] == "compose.yaml,compose.http.yaml"
	assert said == ["Port 80 is free - the http -> https redirect is on."]


def test_prepare_start_refreshes_the_server_ips(home):
	installed(home)
	manage.prepare_start({}, ["10.0.0.9", "192.168.1.20"])
	assert manage.env_read()["NETROLLOUT_SERVER_IPS"] == "10.0.0.9,192.168.1.20"
	manage.prepare_start({}, [])                                   # none found: kept
	assert manage.env_read()["NETROLLOUT_SERVER_IPS"] == "10.0.0.9,192.168.1.20"


HEALTHY = {"status": "ok", "postgres": True, "redis": True, "draining": False,
           "rollouts": {"running": 0, "queued": 0}, "version": "x"}
ALL_UP = ",".join(f"{s}=running/healthy" for s in manage.CORE_SERVICES +
                  manage.MONITORING_SERVICES)


def test_status_when_all_is_well(home):
	installed(home)
	seen = manage.Observed(manage.parse_containers(ALL_UP), reachable=True)
	lines, well = manage.status(seen, HEALTHY)
	assert well
	text = "\n".join(lines)
	assert "Address:      https://nr01.corp.local  (also https://10.0.0.5)" in text
	assert "Containers:   app ok, nginx ok, postgres ok, redis ok, prometheus ok" in text
	assert "Health:       ok (database, Redis)" in text
	assert "Rollouts:     none running" in text
	assert "Certificate:  self-signed, valid until" in text
	assert "Port 80:      redirects to HTTPS" in text
	assert "What to do" not in text
	assert text.isascii()


def test_status_says_what_to_do(home):
	installed(home)
	seen = manage.Observed(manage.parse_containers("app=running/healthy,"
	                                               "postgres=running/healthy,redis=running"),
	                       reachable=False)
	lines, well = manage.status(seen, {**HEALTHY, "redis": False,
	                                   "rollouts": {"running": 2, "queued": 1}})
	text = "\n".join(lines)
	assert not well
	assert "nginx NOT RUNNING" in text and "grafana NOT RUNNING" in text
	assert "Rollouts:     2 running, 1 queued" in text
	assert "Redis unreachable" in text
	assert "Start it: netrollout start  (if it stays down: netrollout logs nginx)" in text


def test_status_when_the_app_does_not_answer(home):
	installed(home, "--monitoring", "n")
	seen = manage.Observed(manage.parse_containers(
		"app=running/unhealthy,nginx=running,postgres=running/healthy,redis=running"),
		reachable=True)
	lines, well = manage.status(seen, None)
	text = "\n".join(lines)
	assert not well and "Health:       the app isn't answering" in text
	assert "app RUNNING/UNHEALTHY" in text and "grafana" not in text   # monitoring off


def test_status_reports_a_reachability_problem_only_when_all_runs(home):
	installed(home)
	seen = manage.Observed(manage.parse_containers(ALL_UP), reachable=False)
	lines, well = manage.status(seen, HEALTHY)
	assert not well
	assert any("check the name (DNS) and the firewall for port 443" in l for l in lines)


def test_status_warns_before_the_certificate_expires(home):
	installed(home)
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir(), days=10)
	lines, well = manage.status(manage.Observed(manage.parse_containers(ALL_UP)), HEALTHY)
	assert not well and any("EXPIRES SOON" in l for l in lines)


def test_status_shows_nginx_rejecting_and_a_pending_port(home):
	installed(home)
	(site_env.folder() / "status.json").write_text(
		'{"state": "rejected", "message": "nginx: [emerg] bad", "time": "t"}')
	site_env.update({site_env.PORT_REQUEST: "8443"})
	lines, well = manage.status(manage.Observed(manage.parse_containers(ALL_UP)), HEALTHY)
	text = "\n".join(lines)
	assert not well and "REJECTED the last change (t): nginx: [emerg] bad" in text
	assert "Port change:  8443 requested, 443 in use - run netrollout apply" in text


def test_status_and_prepare_start_need_an_install(home):
	for command in ("status", "prepare-start"):
		code, out = run([command])
		assert code == 1 and "isn't installed here" in out[0]


def test_status_through_the_cli(home, monkeypatch):
	installed(home)
	monkeypatch.setattr(manage, "fetch_health", lambda url: HEALTHY)
	code, out = run(["status", "--containers", ALL_UP, "--reachable", "yes"])
	assert code == 0 and out[0].startswith("NetRollout ")


# ── after a restore ──

def test_restore_key_puts_the_backups_key_into_env_and_removes_the_handover(home):
	from cryptography.fernet import Fernet
	before = installed(home)["NETROLLOUT_ENCRYPTION_KEY"]
	other = Fernet.generate_key().decode()
	(home / "backups").mkdir(exist_ok=True)
	(home / "backups" / manage.RESTORED_KEY).write_text(other + "\n")

	code, out = run(["restore-key"])
	assert code == 0 and "on another installation" in out[0]
	assert manage.env_read()["NETROLLOUT_ENCRYPTION_KEY"] == other != before
	assert not (home / "backups" / manage.RESTORED_KEY).exists()

	(home / "backups" / manage.RESTORED_KEY).write_text(other)
	assert run(["restore-key"]) == (0, ["The encryption key is unchanged."])


def test_restore_key_refuses_without_a_key(home):
	key = installed(home)["NETROLLOUT_ENCRYPTION_KEY"]
	code, out = run(["restore-key"])
	assert code == 1 and "run the restore first" in out[0]
	(home / "backups").mkdir(exist_ok=True)
	(home / "backups" / manage.RESTORED_KEY).write_text("not a key")
	code, out = run(["restore-key"])
	assert code == 1 and "doesn't hold an encryption key" in out[0]
	assert manage.env_read()["NETROLLOUT_ENCRYPTION_KEY"] == key


def test_status_shows_the_backups_and_a_failed_scheduled_one(home):
	import json
	from tests.unit.test_backup import make_zip
	installed(home)
	seen = manage.Observed(manage.parse_containers(ALL_UP), reachable=True)
	assert "Backups:      none yet" in "\n".join(manage.status(seen, HEALTHY)[0])

	(home / "backups").mkdir(exist_ok=True)
	make_zip(home / "backups", "netrollout-1.0.0-20261005-020000-scheduled.zip")
	(home / "backups" / ".schedule-status.json").write_text(json.dumps(
		{"time": "2026-10-06T02:00:00", "ok": False, "message": "disk full", "file": None}))
	lines, well = manage.status(seen, HEALTHY)
	text = "\n".join(lines)
	assert not well
	assert ("Backups:      1, 0.0 MB in backups, newest 2026-10-05 02:00 - the last "
	        "scheduled one FAILED") in text
	assert "The last scheduled backup failed (disk full): see System Settings -> Backups" in text
