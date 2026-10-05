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
	code, out = run(["init", *WINDOWS, "--busy-ports", "80=IIS"],
	                "yes", "", "", "", "", "")
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
	assert run(["init", "--yes", "--defaults"])[0] == 0
	assert "COMPOSE_FILE=compose.yaml,compose.http.yaml" in \
	       (home / ".env").read_text(encoding="utf-8")


def test_never_twice(home):
	assert run(["init", "--yes", "--defaults"])[0] == 0
	before = (home / ".env").read_bytes()
	code, out = run(["init", "--yes", "--defaults"])
	assert code == 2 and "already installed" in out[0]
	assert (home / ".env").read_bytes() == before


def test_the_licence_must_be_accepted(home):
	code, out = run(["init", *WINDOWS], "no")
	assert code == 1 and "weren't accepted" in out[-1]
	assert not (home / ".env").exists()
	assert run(["init", "--defaults"])[0] == 1             # unattended: --yes


def test_the_licence_names_docker_desktop_on_windows_only(home):
	_, windows = run(["init", "--os", "windows"], "no")
	_, linux = run(["init", "--os", "linux"], "no")
	assert any("Docker Desktop" in s for s in windows)
	assert not any("Docker Desktop" in s for s in linux)


def test_unattended_with_bad_answers_writes_nothing(home):
	code, out = run(["init", "--yes", "--defaults", "--https-port", "80",
	                 "--timezone", "Mars/Olympus"])
	assert code == 1 and len(out) == 2
	assert not (home / ".env").exists()


def test_an_organisation_certificate_must_be_in_place_and_cover_the_name(home):
	argv = ["init", "--yes", "--defaults", "--hostname", "nr01.corp.local",
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
	code, out = run(["init", "--yes", "--defaults"])
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
