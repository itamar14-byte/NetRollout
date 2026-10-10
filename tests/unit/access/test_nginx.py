"""src/access/nginx.py: the values the app hands nginx (site.env) and
the watcher's verdict it reads back (status.json). No nginx here — the files."""
import datetime
import json
import os
import threading
from types import SimpleNamespace

import pytest

from src import runtime
from src.access import certs, nginx as pc, port, site_env


@pytest.fixture
def home(tmp_path, monkeypatch):
	"""NETROLLOUT_HOME in a temp folder, no applied port in the environment."""
	monkeypatch.setenv("NETROLLOUT_HOME", str(tmp_path))
	monkeypatch.delenv(port.PUBLISHED_PORT_ENV, raising=False)
	return tmp_path


def site(home):
	return (home / "config" / "nginx" / "site.env").read_text(encoding="utf-8")


def nginx():
	"""Nginx with the certs folder (NETROLLOUT_HOME's)."""
	return pc.Nginx(certs.CertificateStore())


def test_writes_the_hostname_and_the_applied_port(home, monkeypatch):
	"""write_site writes site.env with exactly the hostname and the applied port
	(NETROLLOUT_HTTPS_PORT from the environment), and says it changed."""
	monkeypatch.setenv(port.PUBLISHED_PORT_ENV, "8443")
	assert pc.write_site("nr01.corp.local") is True
	assert site(home) == ("NETROLLOUT_HOSTNAME=nr01.corp.local\n"
	                      "NETROLLOUT_HTTPS_PORT=8443\n")


def test_no_hostname_means_no_canonical_name(home):
	"""An empty or None hostname writes an empty NETROLLOUT_HOSTNAME, with the default
	port 443."""
	pc.write_site("")
	pc.write_site(None)
	assert site(home) == "NETROLLOUT_HOSTNAME=\nNETROLLOUT_HTTPS_PORT=443\n"


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


# ── Undo: only what this change wrote, under the certificate lock ────────────

def certificate_for(home):
	"""The name the certificate in the certs folder was issued to."""
	return certs.common_name((home / "certs" / certs.CERT_FILE).read_bytes())


def test_a_hostname_undo_puts_back_only_the_hostname(home):
	"""Undoing a hostname change puts the previous hostname back in site.env and
	keeps the keys written meanwhile (a port request, its confirmation) - it
	used to put the whole file back, dropping them."""
	pc.write_site("a.lab")
	undo = nginx().change_hostname("b.lab")
	site_env.update({site_env.PORT_REQUEST: "8443", site_env.PORT_REQUEST_ID: "r1",
	                 site_env.PORT_CONFIRMED: "r1"})
	undo()
	values = site_env.read()
	assert values[site_env.HOSTNAME] == "a.lab"
	assert (values[site_env.PORT_REQUEST], values[site_env.PORT_REQUEST_ID],
	        values[site_env.PORT_CONFIRMED]) == ("8443", "r1", "r1")


def test_a_hostname_undo_keeps_a_port_kept_since(home):
	"""The port in use written by the helper after a hostname change (a kept
	port) survives the hostname's undo; the hostname goes back."""
	pc.write_site("a.lab")
	undo = nginx().change_hostname("b.lab")
	site_env.update({site_env.PORT_IN_USE: "8443"})
	undo()
	values = site_env.read()
	assert (values[site_env.HOSTNAME], values[site_env.PORT_IN_USE]) == ("a.lab", "8443")


def test_an_undo_leaves_what_a_later_change_wrote(home):
	"""Change A, then change B, then A's undo: B's certificate and hostname stay
	(A's undo used to put A's whole snapshot back over them); B's own undo still
	works."""
	certs.selfsigned("a.lab", ["10.0.0.5"], home / "certs")
	pc.write_site("a.lab")
	undo_a = nginx().change_hostname("b.lab")
	undo_b = nginx().change_hostname("c.lab")
	undo_a()
	assert certificate_for(home) == "c.lab"
	assert site_env.read()[site_env.HOSTNAME] == "c.lab"
	undo_b()
	assert certificate_for(home) == "b.lab"
	assert site_env.read()[site_env.HOSTNAME] == "b.lab"


def test_a_certificate_undo_leaves_a_later_certificate(home):
	"""Generate a self-signed certificate (A), then another (B), then A's undo:
	B's stays; B's undo puts A's back."""
	certs.selfsigned("old.lab", [], home / "certs")
	undo_a = certs.CertificateStore().generate_selfsigned("a.lab", pc.server_ips())
	undo_b = certs.CertificateStore().generate_selfsigned("b.lab", pc.server_ips())
	undo_a()
	assert certificate_for(home) == "b.lab"
	undo_b()
	assert certificate_for(home) == "a.lab"


@pytest.mark.parametrize("change", ["hostname", "generate"])
def test_an_undo_waits_for_the_certificate_lock(home, change):
	"""An undo runs under the certificate lock: while another change (or the
	upkeep) holds it, the undo waits."""
	certs.selfsigned("a.lab", [], home / "certs")
	undo = nginx().change_hostname("b.lab") if change == "hostname" \
		else certs.CertificateStore().generate_selfsigned("b.lab", pc.server_ips())
	with certs.CertificateStore.lock:
		worker = threading.Thread(target=undo)
		worker.start()
		worker.join(timeout=0.5)
		assert worker.is_alive()          # waiting for the lock
		assert certificate_for(home) == "b.lab"
	worker.join(timeout=10)
	assert not worker.is_alive() and certificate_for(home) == "a.lab"


# ── A change that fails half way: nothing left changed ───────────────────────

def cert_files(home):
	"""The certificate folder's files and their bytes."""
	return {p.name: p.read_bytes() for p in (home / "certs").iterdir() if p.is_file()}


def test_a_hostname_whose_site_env_cant_be_written_changes_nothing(home, monkeypatch):
	"""change_hostname with a self-signed certificate: the certificate is reissued
	first, then site.env's write raises OSError -> ProxyError "NetRollout couldn't
	write <file>: <reason>. Nothing was changed.", the certificate files (and no
	old-names file) exactly as before, site.env's hostname kept."""
	certs.selfsigned("a.lab", ["10.0.0.5"], home / "certs")
	pc.write_site("a.lab")
	before = cert_files(home)

	def fail(hostname):
		raise PermissionError(13, "Permission denied", "/shared/site.env")
	monkeypatch.setattr(pc, "write_site", fail)
	with pytest.raises(certs.ProxyError) as e:
		nginx().change_hostname("b.lab")
	assert str(e.value) == ("NetRollout couldn't write /shared/site.env: "
	                        "Permission denied. Nothing was changed.")
	assert cert_files(home) == before and certificate_for(home) == "a.lab"
	assert site_env.read()[site_env.HOSTNAME] == "a.lab"


def test_an_unnamed_write_failure_names_the_shared_folder(home, monkeypatch):
	"""An OSError without a file name or reason is reported against nginx's shared
	folder, with the error's own text."""
	pc.write_site("a.lab")

	def fail(hostname):
		raise OSError("disk gone")
	monkeypatch.setattr(pc, "write_site", fail)
	with pytest.raises(certs.ProxyError) as e:
		nginx().change_hostname("b.lab")
	assert str(e.value) == (f"NetRollout couldn't write {site_env.folder()}: "
	                        f"disk gone. Nothing was changed.")


def test_a_value_error_during_a_hostname_change_changes_nothing(home, monkeypatch):
	"""A ValueError from the site.env step -> ProxyError "<its message>. Nothing was
	changed.", with the reissued certificate put back."""
	certs.selfsigned("a.lab", [], home / "certs")
	pc.write_site("a.lab")
	before = cert_files(home)

	def fail(hostname):
		raise ValueError("Hostname 'b.lab' isn't allowed")
	monkeypatch.setattr(pc, "write_site", fail)
	with pytest.raises(certs.ProxyError) as e:
		nginx().change_hostname("b.lab")
	assert str(e.value) == "Hostname 'b.lab' isn't allowed. Nothing was changed."
	assert cert_files(home) == before and certificate_for(home) == "a.lab"


def test_an_invalid_hostname_is_refused_without_writing(home):
	"""An invalid hostname (no certificate in use) is a ProxyError ending in
	"Nothing was changed."; site.env keeps the previous hostname."""
	pc.write_site("a.lab")
	with pytest.raises(certs.ProxyError, match=r"\. Nothing was changed\.$"):
		nginx().change_hostname("not a hostname!")
	assert site_env.read()[site_env.HOSTNAME] == "a.lab"


def organisation_pair(tmp_path, name):
	"""(certificate, key) PEM bytes for `name`, made outside the certs folder (so
	no self-signed marker comes with them)."""
	folder = tmp_path / f"org-{name}"
	certs.selfsigned(name, [], folder)
	return (folder / certs.CERT_FILE).read_bytes(), (folder / certs.KEY_FILE).read_bytes()


def test_an_organisation_certificate_that_cant_be_written_changes_nothing(
		home, tmp_path, monkeypatch):
	"""CertificateStore.install: the key is written, then the certificate's write raises
	OSError -> the previous files are put back (the key, the certificate, the
	self-signed marker) and ProxyError "NetRollout couldn't write <file>: <reason>.
	Nothing was changed."."""
	certs.selfsigned("a.lab", [], home / "certs")
	before = cert_files(home)
	cert_pem, key_pem = organisation_pair(tmp_path, "org.lab")
	store = certs.CertificateStore()
	real_restore = store._restore

	def restore(saved):
		if saved == {home / "certs" / certs.CERT_FILE: cert_pem}:
			raise PermissionError(13, "Access is denied", str(home / "certs" / certs.CERT_FILE))
		real_restore(saved)
	monkeypatch.setattr(store, "_restore", restore)
	with pytest.raises(certs.ProxyError) as e:
		store.install(cert_pem, key_pem, "org.lab")
	assert str(e.value) == (f"NetRollout couldn't write {home / 'certs' / certs.CERT_FILE}: "
	                        f"Access is denied. Nothing was changed.")
	assert cert_files(home) == before
	assert certs.is_selfsigned(home / "certs")


def test_an_organisation_certificate_is_installed_without_the_marker(home, tmp_path):
	"""CertificateStore.install with a valid pair: written in place of the self-signed
	one, the marker and old-names file removed; the undo puts the self-signed
	files back."""
	certs.selfsigned("a.lab", [], home / "certs")
	(home / "certs" / certs.OLD_NAMES_FILE).write_text('{"x.lab": 1}', encoding="utf-8")
	before = cert_files(home)
	cert_pem, key_pem = organisation_pair(tmp_path, "org.lab")
	check, undo = certs.CertificateStore().install(cert_pem, key_pem, "org.lab")
	assert check.ok
	assert cert_files(home) == {certs.CERT_FILE: cert_pem, certs.KEY_FILE: key_pem}
	undo()
	assert cert_files(home) == before


# ── overview(): the certificate the Access card shows ────────────────────────

def test_overview_without_a_certificate(home):
	"""No certificate file and no nginx status: both None."""
	assert nginx().overview("a.lab") == {"nginx": None, "certificate": None}


def test_overview_when_the_certificate_cant_be_read(home):
	"""A certificate that exists but can't be read (here a folder in its place) is
	shown as one problem "NetRollout can't read the certificate: <reason>." with
	no names, no expiry, not self-signed, no warnings or old names."""
	(home / "certs" / certs.CERT_FILE).mkdir(parents=True)
	cert = nginx().overview("a.lab")["certificate"]
	assert (cert["names"], cert["not_after"], cert["selfsigned"], cert["old_names"],
	        cert["warnings"]) == ([], None, False, [], [])
	(problem,) = cert["problems"]
	assert problem.startswith("NetRollout can't read the certificate: ")
	assert problem.endswith(".")


def test_overview_with_the_key_missing(home):
	"""A certificate without its key: the names and the self-signed state are
	shown, and validate's problem for an empty key ("The key file isn't a PEM
	private key.") is the only problem."""
	certs.selfsigned("a.lab", ["10.0.0.5"], home / "certs")
	(home / "certs" / certs.KEY_FILE).unlink()
	cert = nginx().overview("a.lab")["certificate"]
	assert cert["names"] == ["a.lab", "10.0.0.5"]
	assert cert["selfsigned"] is True
	assert cert["problems"] == ["The key file isn't a PEM private key."]


def test_seeding_ignores_an_unreadable_site_env(home, monkeypatch):
	"""seed_hostname_from_site when site.env can't be read (OSError): nothing
	happens - the seed stays as it was."""
	monkeypatch.setenv(pc.HOSTNAME_SEED_ENV, "")

	def unreadable():
		raise PermissionError(13, "Permission denied")
	monkeypatch.setattr(site_env, "read", unreadable)
	pc.seed_hostname_from_site()
	assert os.environ[pc.HOSTNAME_SEED_ENV] == ""


# ── The upkeep thread (CertificateUpkeep), driven turn by turn ───────────────

class _Stop(Exception):
	"""Ends the loop: raised by the fake sleep."""


def test_the_upkeep_drops_names_every_hour_and_survives_failures(monkeypatch, capsys):
	"""The thread (daemon, "certificate-upkeep") calls the store's drop_expired() at
	once and after every UPKEEP_INTERVAL_SECONDS sleep: nothing dropped prints
	nothing; names dropped print the reissue line; an exception prints
	"certificate upkeep failed: ..." and the loop goes on."""
	thread, events = {}, []
	outcomes = iter([[], ["old.lab", "older.lab"], RuntimeError("no certs folder"), []])

	class FakeThread:
		def __init__(self, **kwargs):
			thread.update(kwargs)

		def start(self):
			thread["started"] = True

	def sleep(seconds):
		events.append(("sleep", seconds))
		if len(events) == 8:
			raise _Stop

	def drop():
		events.append(("drop",))
		outcome = next(outcomes)
		if isinstance(outcome, Exception):
			raise outcome
		return outcome

	monkeypatch.setattr(runtime.threading, "Thread", FakeThread)
	monkeypatch.setattr(runtime, "time", SimpleNamespace(sleep=sleep))
	store = certs.CertificateStore()
	monkeypatch.setattr(store, "drop_expired", drop)
	certs.CertificateUpkeep(store).start()
	assert thread["started"] and thread["daemon"] is True
	assert thread["name"] == "certificate-upkeep"
	with pytest.raises(_Stop):
		thread["target"]()
	assert events == [("drop",), ("sleep", certs.UPKEEP_INTERVAL_SECONDS)] * 4
	assert capsys.readouterr().out == (
		"[NetRollout] certificate reissued without the previous hostname(s) "
		f"old.lab, older.lab (transition of {certs.NAME_TRANSITION_DAYS} days over)\n"
		"[NetRollout] certificate upkeep failed: no certs folder\n")
