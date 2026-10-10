"""src/setup: the install questions, their checks and defaults, what an
install writes, and the contract with the host scripts (exit codes)."""
import datetime
import json

import pytest
from cryptography.fernet import Fernet

from src import runtime
from src.access import certs, site_env
from src.setup import __main__ as cli, install, update as _update
from src.setup import manage  # noqa: E402
from src.setup.env import DEV_COMPOSE, env_read, env_set, env_text, generate_secrets, UPGRADE_DEFAULTS
from tests.unit.backup.test_archive import make_zip


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
	"""The setup CLI run with `argv`, answering `replies`: (exit code, the lines it wrote)."""
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
	"""A hostname is accepted lower-cased and without a trailing dot; an IP as given."""
	assert install.check_hostname(value) == expected


@pytest.mark.parametrize("bad", ["", "https://nr01", "nr 01", "nr01:8443"])
def test_bad_hostnames(bad):
	"""Empty, a URL, a space or a port in the hostname is invalid."""
	with pytest.raises(install.Invalid):
		install.check_hostname(bad)


def test_a_busy_port_names_who_and_suggests_a_free_one():
	"""A busy port is refused naming its owner and suggesting a free one (9443); a free
	port is accepted as a number; 80, 0, 70000 and a non-number are invalid."""
	busy = {443: "Windows' HTTP service", 8443: ""}
	with pytest.raises(install.Invalid) as e:
		install.check_port("443", busy)
	assert "in use on this computer (by Windows' HTTP service)" in str(e.value)
	assert "e.g. 9443" in str(e.value)
	assert install.check_port("9443", busy) == 9443
	for bad in ("80", "0", "70000", "https"):
		with pytest.raises(install.Invalid):
			install.check_port(bad, {})


@pytest.mark.parametrize("value, expected", [
	("Israel Standard Time", "Asia/Jerusalem"),   # Windows -> IANA (CLDR)
	("W. Europe Standard Time", "Europe/Berlin"),
	("Asia/Jerusalem", "Asia/Jerusalem"), ("UTC", "UTC")])
def test_timezones(value, expected):
	"""A Windows timezone name becomes its IANA name; an IANA name or UTC stays as it is."""
	assert install.check_timezone(value) == expected


def test_an_unknown_timezone():
	"""A timezone that is neither Windows' nor IANA's is invalid."""
	with pytest.raises(install.Invalid):
		install.check_timezone("Mars/Olympus")


def test_defaults_come_from_the_host():
	"""Unattended, the answers default to the computer name, a free port (8443 when 443 is
	busy), monitoring on, no organisation certificate and the host's timezone; a computer
	name that isn't a hostname gives `netrollout`, an unknown timezone UTC."""
	facts = install.Facts(computer_name="NR-SRV01", timezone="Israel Standard Time",
	                busy_ports={443: ""})
	answers = install.collect(facts, {}, interactive=False)
	assert answers == install.Answers("nr-srv01", 8443, True, False, "Asia/Jerusalem")
	# a computer name that isn't a hostname, an unknown timezone
	plain = install.collect(install.Facts(computer_name="My PC!", timezone="?"), {}, False)
	assert (plain.hostname, plain.timezone) == ("netrollout", "UTC")


# ── asking ──

def test_enter_accepts_the_default_and_a_wrong_answer_is_asked_again():
	"""Asked interactively, Enter takes the default shown in brackets; a busy port and a
	yes/no answer that is neither are explained and asked again."""
	read, said = scripted("", "443", "8443", "maybe", "n", "", ""), []
	answers = install.collect(install.Facts(computer_name="box", busy_ports={443: ""}), {},
	                    True, read, said.append)
	assert read.asked[0] == "Hostname people will use [box]: "
	assert read.asked[1] == "HTTPS port [8443]: "            # 443 taken
	assert answers == install.Answers("box", 8443, False, False, "UTC")
	assert any("Port 443 is in use" in s for s in said)
	assert "  Answer y or n." in said


def test_input_ending_is_not_an_answer():
	"""Input that ends (EOF) while asking raises NoAnswer instead of taking a default."""
	with pytest.raises(install.NoAnswer):
		install.collect(install.Facts(), {}, True, scripted(), print)


def test_given_answers_are_checked_all_at_once():
	"""Several bad given answers (hostname and port) are reported together, one per line."""
	with pytest.raises(install.Invalid) as e:
		install.collect(install.Facts(busy_ports={443: ""}),
		          {"hostname": "https://x", "https_port": "443"}, False)
	assert str(e.value).count("\n") == 1                  # both reported


# ── the install ──

def test_an_install_writes_everything(home):
	"""`init` creates .env (the 13 keys; 7 strong, distinct secrets, alphanumeric passwords;
	the header names the account), logs/, config/, certs/ and backups/; port 80 busy means
	no redirect file; site.env gets the hostname and port, the certificate the IPs."""
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
	                           site_env.PORT_IN_USE: "443"}
	dns, ips = certs.names_in((home / "certs" / certs.CERT_FILE).read_bytes())
	assert dns == ["nr-srv01"] and [str(i) for i in ips] == ["10.0.0.5", "192.168.1.20"]


def test_port_80_free_adds_the_redirect(home):
	"""With port 80 free, COMPOSE_FILE includes compose.http.yaml (the http -> https
	redirect)."""
	assert run(["init", "--licence-accepted", "--defaults"])[0] == 0
	assert "COMPOSE_FILE=compose.yaml,compose.http.yaml" in \
	       (home / ".env").read_text(encoding="utf-8")


def test_never_twice(home):
	"""A second `init` exits 2 ("already installed") and leaves .env as it was."""
	assert run(["init", "--licence-accepted", "--defaults"])[0] == 0
	before = (home / ".env").read_bytes()
	code, out = run(["init", "--licence-accepted", "--defaults"])
	assert code == 2 and "already installed" in out[0]
	assert (home / ".env").read_bytes() == before


def test_init_needs_the_scripts_licence_acceptance(home):
	"""Without --licence-accepted `init` exits 1 and writes no .env, asked or unattended.
	The notice is the scripts' (shown before Docker is installed)."""
	code, out = run(["init", *WINDOWS], "", "", "", "", "")
	assert code == 1 and "licence notice wasn't accepted" in out[-1]
	assert not (home / ".env").exists()
	assert run(["init", "--defaults"])[0] == 1


def test_unattended_with_bad_answers_writes_nothing(home):
	"""An unattended `init` with a bad port and timezone exits 1, saying two lines, and
	writes no .env."""
	code, out = run(["init", "--licence-accepted", "--defaults", "--https-port", "80",
	                 "--timezone", "Mars/Olympus"])
	assert code == 1 and len(out) == 2
	assert not (home / ".env").exists()


def test_an_organisation_certificate_must_be_in_place_and_cover_the_name(home):
	"""With --org-certificate y, `init` refuses without a certificate in certs/ and with
	one that doesn't cover the hostname; with a covering one it installs and keeps it."""
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
	"""`check` prints "ok" for good answers and exits 1 for a bad port, writing nothing."""
	code, out = run(["check", "--hostname", "nr01", "--https-port", "8443"])
	assert (code, out) == (0, ["ok"])
	assert list(home.iterdir()) == []
	assert run(["check", "--https-port", "80"])[0] == 1


def test_a_write_failure_leaves_no_env(home, monkeypatch):
	"""A permission error while writing the certificate exits 1 naming it, with no .env left."""
	def denied(*a, **k):
		raise PermissionError(13, "Permission denied", str(home / "certs"))
	monkeypatch.setattr(certs, "selfsigned", denied)
	code, out = run(["init", "--licence-accepted", "--defaults"])
	assert code == 1 and "Permission denied" in out[-1]
	assert not (home / ".env").exists()


def test_the_env_text_is_stable():
	"""The .env text starts with the version and time header, names Docker Engine's licence
	(Linux) and has an empty COMPOSE_PROFILES with monitoring off."""
	answers = install.Answers("nr01", 8443, False, False, "UTC")
	keys = {k: "x" for k in generate_secrets()}
	text = env_text(answers, install.Facts(os="linux"), keys,
	                      datetime.datetime(2026, 10, 5, 12, 0), "1.0.0", True)
	assert text.startswith("# NetRollout 1.0.0 — written by the installer "
	                       "on 2026-10-05 12:00.\n")
	assert "Docker Engine (Apache-2.0)" in text and "COMPOSE_PROFILES=\n" in text


# ── a developer's setup ──

def test_init_dev(home):
	"""`init --dev` writes the dev stack's .env, a runtime.env pointing the host app at
	127.0.0.1 with the app's DB password (no encryption key: the dev key file stays) and
	a localhost certificate; a second run exits 2."""
	code, out = run(["init", "--dev"])
	assert code == 0
	env = (home / ".env").read_text(encoding="utf-8")
	assert "NETROLLOUT_VERSION=dev" in env
	assert f"COMPOSE_FILE={DEV_COMPOSE}" in env
	app = (home / "config" / "runtime.env").read_text(encoding="utf-8")
	db_password = next(line.split("=", 1)[1] for line in env.splitlines()
	                   if line.startswith("NETROLLOUT_DB_PASSWORD="))
	assert f"PG_PASSWORD={db_password}" in app and "PG_HOST=127.0.0.1" in app
	assert "NETROLLOUT_ENCRYPTION_KEY" not in app      # the dev key file stays
	assert certs.names_in((home / "certs" / certs.CERT_FILE).read_bytes())[0] == ["localhost"]
	assert run(["init", "--dev"])[0] == 2               # never twice






def installed(home, *extra):
	"""An unattended install for nr01.corp.local (IP 10.0.0.5); returns its .env values."""
	assert run(["init", "--licence-accepted", "--defaults", "--hostname",
	            "nr01.corp.local", "--server-ips", "10.0.0.5", *extra])[0] == 0
	return env_read()


def test_env_set_edits_in_place_and_only_script_keys(home):
	"""env_set changes only the key's line (comments kept), returns False when nothing
	changes, and refuses a key the scripts don't own (SECRET_KEY)."""
	installed(home)
	before = (home / ".env").read_text(encoding="utf-8")
	assert env_set({"TZ": "Europe/London"}) is True
	after = (home / ".env").read_text(encoding="utf-8")
	assert after == before.replace("TZ=UTC\n", "TZ=Europe/London\n")   # comments kept
	assert env_set({"TZ": "Europe/London"}) is False
	with pytest.raises(ValueError):
		env_set({"SECRET_KEY": "x"})


def test_port_80_taken_later_turns_the_redirect_off_and_back_on(home):
	"""prepare-start drops compose.http.yaml when port 80 is now busy (saying who has it),
	says nothing when it is already off, and puts it back once port 80 is free."""
	env = installed(home)
	assert env["COMPOSE_FILE"] == "compose.yaml,compose.http.yaml"
	said = manage.prepare_start({80: "Windows' HTTP service"}, ["10.0.0.5"])
	assert env_read()["COMPOSE_FILE"] == "compose.yaml"
	assert said == ["Port 80 is in use (by Windows' HTTP service) - starting without "
	                "the http -> https redirect (it comes back by itself once port 80 "
	                "is free)."]
	assert manage.prepare_start({80: "x"}, ["10.0.0.5"]) == []     # already off
	said = manage.prepare_start({}, ["10.0.0.5"])
	assert env_read()["COMPOSE_FILE"] == "compose.yaml,compose.http.yaml"
	assert said == ["Port 80 is free - the http -> https redirect is on."]


def test_prepare_start_refreshes_the_server_ips(home):
	"""prepare-start writes the server's current IPs, and keeps the old ones when none
	are found."""
	installed(home)
	manage.prepare_start({}, ["10.0.0.9", "192.168.1.20"])
	assert env_read()["NETROLLOUT_SERVER_IPS"] == "10.0.0.9,192.168.1.20"
	manage.prepare_start({}, [])                                   # none found: kept
	assert env_read()["NETROLLOUT_SERVER_IPS"] == "10.0.0.9,192.168.1.20"


HEALTHY = {"status": "ok", "postgres": True, "redis": True, "draining": False,
           "rollouts": {"running": 0, "queued": 0}, "version": "x"}
ALL_UP = ",".join(f"{s}=running/healthy" for s in manage.CORE_SERVICES +
                  manage.MONITORING_SERVICES)


def test_status_when_all_is_well(home):
	"""With every container up and a healthy app, status is well and its ASCII lines show
	the address, containers, health, rollouts, certificate and port 80, no What to do."""
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
	"""Missing containers are shown NOT RUNNING, the rollouts counted, Redis unreachable,
	and the next step given (netrollout start, then the nginx logs)."""
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
	"""No health answer says the app isn't answering and shows it unhealthy; with
	monitoring off, Grafana isn't mentioned."""
	installed(home, "--monitoring", "n")
	seen = manage.Observed(manage.parse_containers(
		"app=running/unhealthy,nginx=running,postgres=running/healthy,redis=running"),
		reachable=True)
	lines, well = manage.status(seen, None)
	text = "\n".join(lines)
	assert not well and "Health:       the app isn't answering" in text
	assert "app RUNNING/UNHEALTHY" in text and "grafana" not in text   # monitoring off


def test_status_reports_a_reachability_problem_only_when_all_runs(home):
	"""With everything running but the address unreachable, status is not well and points
	at DNS and the firewall for the port."""
	installed(home)
	seen = manage.Observed(manage.parse_containers(ALL_UP), reachable=False)
	lines, well = manage.status(seen, HEALTHY)
	assert not well
	assert any("check the name (DNS) and the firewall for port 443" in l for l in lines)


def test_status_warns_before_the_certificate_expires(home):
	"""A certificate valid 10 more days makes status not well, with EXPIRES SOON."""
	installed(home)
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir(), days=10)
	lines, well = manage.status(manage.Observed(manage.parse_containers(ALL_UP)), HEALTHY)
	assert not well and any("EXPIRES SOON" in l for l in lines)


def test_a_missing_certificate_is_reported_without_reading_site_env(home, monkeypatch):
	"""With no certificate files the Certificate line is MISSING with its next step
	even when site.env can't be read (PermissionError) - the hostname is only read
	for a certificate to check, so nothing raises."""
	def unreadable():
		raise PermissionError(13, "Permission denied")
	monkeypatch.setattr(site_env, "read", unreadable)
	todo = []
	assert manage._certificate(datetime.datetime.now(datetime.timezone.utc), todo) == "MISSING"
	assert todo == ["No certificate: upload one in Server Management, or generate a "
	                "self-signed one there"]


def test_status_shows_nginx_rejecting_and_a_pending_port(home):
	"""nginx's rejected verdict is shown with its message and time, and a requested port
	with the one in use and `netrollout apply`."""
	installed(home)
	(site_env.folder() / "status.json").write_text(
		'{"state": "rejected", "message": "nginx: [emerg] bad", "time": "t"}')
	site_env.update({site_env.PORT_REQUEST: "8443"})
	lines, well = manage.status(manage.Observed(manage.parse_containers(ALL_UP)), HEALTHY)
	text = "\n".join(lines)
	assert not well and "REJECTED the last change (t): nginx: [emerg] bad" in text
	assert "Port change:  8443 requested, 443 in use - run netrollout apply" in text


def test_status_and_prepare_start_need_an_install(home):
	"""`status` and `prepare-start` without an install exit 1 saying it isn't installed here."""
	for command in ("status", "prepare-start"):
		code, out = run([command])
		assert code == 1 and "isn't installed here" in out[0]


def test_status_through_the_cli(home, monkeypatch):
	"""`status --containers … --reachable yes --health-url U` asks U for the app's health
	and prints exactly manage.status's report for what it was given, exiting 0 when all
	is well; with the address not answering (`--reachable no`) and nginx down it exits 1,
	its report saying both."""
	installed(home)
	asked = []
	monkeypatch.setattr(manage, "fetch_health",
	                    lambda url=manage.HEALTH_URL, timeout=4.0: asked.append(url) or HEALTHY)
	url = "https://127.0.0.1:9443/_netrollout/health"
	code, out = run(["status", "--containers", ALL_UP, "--reachable", "yes", "--health-url", url])
	assert asked == [url]
	lines, well = manage.status(manage.Observed(manage.parse_containers(ALL_UP), reachable=True),
	                            HEALTHY)
	assert well and code == 0 and out == lines

	nginx_down = ALL_UP.replace("nginx=running/healthy", "nginx=exited")
	code, out = run(["status", "--containers", nginx_down, "--reachable", "no"])
	text = "\n".join(out)
	assert code == 1
	assert "nginx EXITED" in text and "does NOT answer" in text


# ── after a restore ──

def test_restore_key_puts_the_backups_key_into_env_and_removes_the_handover(home):
	"""`restore-key` writes the restored key into .env, says it came from another
	installation and removes the handover file; the same key again is "unchanged"."""
	before = installed(home)["NETROLLOUT_ENCRYPTION_KEY"]
	other = Fernet.generate_key().decode()
	(home / "backups").mkdir(exist_ok=True)
	(home / "backups" / manage.RESTORED_KEY).write_text(other + "\n")

	code, out = run(["restore-key"])
	assert code == 0 and "on another installation" in out[0]
	assert env_read()["NETROLLOUT_ENCRYPTION_KEY"] == other != before
	assert not (home / "backups" / manage.RESTORED_KEY).exists()

	(home / "backups" / manage.RESTORED_KEY).write_text(other)
	assert run(["restore-key"]) == (0, ["The encryption key is unchanged."])


def test_restore_key_refuses_without_a_key(home):
	"""`restore-key` exits 1 without a handover file (run the restore first) or with one
	that holds no key, and .env's key stays."""
	key = installed(home)["NETROLLOUT_ENCRYPTION_KEY"]
	code, out = run(["restore-key"])
	assert code == 1 and "run the restore first" in out[0]
	(home / "backups").mkdir(exist_ok=True)
	(home / "backups" / manage.RESTORED_KEY).write_text("not a key")
	code, out = run(["restore-key"])
	assert code == 1 and "doesn't hold an encryption key" in out[0]
	assert env_read()["NETROLLOUT_ENCRYPTION_KEY"] == key


def test_status_shows_the_backups_and_a_failed_scheduled_one(home):
	"""The Backups line says "none yet", then the count, size and newest; a failed
	scheduled backup makes status not well, with its reason and where to look."""
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


# ── an update ──

@pytest.mark.parametrize("installed, new, kind", [
	("1.0.0", "1.0.1", "update"),
	("1.0.0", "1.1.0", "update"),           # versions skipped: migrations run in order
	("1.0.0.dev0", "1.0.0rc1", "update"),   # dev < release candidate < release
	("1.0.0rc1", "1.0.0", "update"),
	("1.0.0", "1.0.1.dev0", "update"),
])
def test_an_update_goes_forward(installed, new, kind):
	"""A newer version (PEP 440: dev < rc < release; versions may be skipped) is an
	"update"."""
	assert _update.update_kind(installed, new) == kind


@pytest.mark.parametrize("installed, new", [("1.0.0", "1.0.0"), ("1.0.0.dev2", "1.0.0.dev2"),
                                            ("1.0.0", "v1.0.0")])
def test_the_same_version_has_nothing_to_update(installed, new):
	"""The same version again (Setup run twice, `update` to the installed release) is
	refused - nothing to update - not offered as an update to itself."""
	with pytest.raises(ValueError, match=f"NetRollout {installed} is already installed - the same "
	                                     f"version. Nothing to update."):
		_update.update_kind(installed, new)


@pytest.mark.parametrize("installed, new", [("1.0.1", "1.0.0"), ("1.0.0", "1.0.0rc1"),
                                            ("1.0.0", "1.0.0.dev0")])
def test_never_back_to_an_older_version(installed, new):
	"""An older version (a lower patch, an rc or a dev of the installed release) is
	refused, saying the installed one is newer, that downgrades aren't supported,
	and how to run an earlier version (uninstall, that version, its backup)."""
	with pytest.raises(ValueError, match=f"NetRollout {installed} is installed - newer than {new}. "
	                                     f"Downgrades aren't supported.") as refused:
		_update.update_kind(installed, new)
	assert "uninstall NetRollout, then install that version" in str(refused.value)
	assert "a backup made by that version" in str(refused.value)


def test_check_update_through_the_cli():
	"""`check-update` prints "update" (exit 0), refuses an older one and the same one
	with exit 2 (the scripts stop: nothing changed), and exits 1 without --new."""
	assert run(["check-update", "--installed", "1.0.0", "--new", "1.0.1"]) == (0, ["update"])
	code, out = run(["check-update", "--installed", "1.0.1", "--new", "1.0.0"])
	assert code == 2 and "newer than 1.0.0" in out[0]
	code, out = run(["check-update", "--installed", "1.0.0", "--new", "1.0.0"])
	assert code == 2 and "already installed - the same version" in out[0]
	assert run(["check-update", "--installed", "1.0.0"])[0] == 1


def test_upgrade_adds_what_is_missing_and_keeps_everything_else(home):
	"""`upgrade` adds the missing keys with their defaults at the end and an "Updated to"
	line under the header, keeping every other value; the next update replaces that
	line and adds nothing twice."""
	installed(home)
	path = home / ".env"
	before = path.read_text(encoding="utf-8")
	# an install from a version without TZ and SERVER_IPS (say)
	path.write_text("".join(l for l in before.splitlines(True)
	                        if not l.startswith(("TZ=", "NETROLLOUT_SERVER_IPS="))),
	                encoding="utf-8")
	said = _update.upgrade("1.0.1", datetime.datetime(2026, 11, 1, 9, 30))
	assert said == [".env: added TZ, NETROLLOUT_SERVER_IPS"]
	after = path.read_text(encoding="utf-8")
	lines = after.splitlines()
	assert lines[0] == before.splitlines()[0]                    # the install's header
	assert lines[1] == "# Updated to NetRollout 1.0.1 on 2026-11-01 09:30 UTC."
	assert after.endswith("# Added by the update to NetRollout 1.0.1\nTZ=UTC\n"
	                      "NETROLLOUT_SERVER_IPS=\n")
	env = env_read()
	for key, value in dotenv(before).items():      # every value kept
		if key not in ("TZ", "NETROLLOUT_SERVER_IPS"):
			assert env[key] == value, key

	# the next update: the stamp replaced, nothing added twice
	assert _update.upgrade("1.0.2", datetime.datetime(2026, 12, 1, 8, 0)) == []
	again = path.read_text(encoding="utf-8").splitlines()
	assert again[1] == "# Updated to NetRollout 1.0.2 on 2026-12-01 08:00 UTC."
	assert sum(l.startswith("# Updated to") for l in again) == 1
	assert sum(l.startswith("TZ=") for l in again) == 1


def test_upgrade_never_makes_up_a_secret_the_data_depends_on(home):
	"""A .env without the encryption key makes `upgrade` exit 1 naming it, writing nothing."""
	installed(home)
	path = home / ".env"
	damaged = "".join(l for l in path.read_text(encoding="utf-8").splitlines(True)
	                  if not l.startswith("NETROLLOUT_ENCRYPTION_KEY="))
	path.write_text(damaged, encoding="utf-8")
	code, out = run(["upgrade"])
	assert code == 1 and "missing NETROLLOUT_ENCRYPTION_KEY - the data depends on it" in out[0]
	assert path.read_text(encoding="utf-8") == damaged            # nothing written


def test_every_env_key_has_an_update_rule(home):
	"""Every key an install writes is in files.UPGRADE_DEFAULTS, and nothing more: a key
	added to the .env template needs a rule for an older install that lacks it."""
	written = set(installed(home))
	assert written == set(UPGRADE_DEFAULTS)


def dotenv(text):
	"""The KEY=value pairs of a .env text, comments skipped."""
	values = {}
	for line in text.splitlines():
		key, sep, value = line.partition("=")
		if sep and key and not key.startswith("#"):
			values[key] = value
	return values
