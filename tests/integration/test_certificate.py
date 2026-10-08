"""Server Management → TLS Certificate: an organisation's certificate
uploaded, or a self-signed one generated — checked first, applied all or
nothing (nginx's verdict included), audited."""
import io
import json

import pytest

from src import runtime
from src.access import certs, nginx as pc
from src.db.tables import AuditLog
from tests.integration.test_admin_settings import admin, proxy  # noqa: F401 — fixtures
from tests.unit.access.test_certs import key_pem, make_cert, pem

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def upload(client, cert_bytes, key_bytes):
	"""POSTs a certificate and a key to the upload route as a multipart form."""
	data = {"certificate": (io.BytesIO(cert_bytes), "fullchain.pem"),
	        "key": (io.BytesIO(key_bytes), "privkey.pem")}
	return client.post("/admin/server/certificate", data=data,
	                   content_type="multipart/form-data")


def org_pair(names=("nr01.corp.local",), ips=()):
	cert, key = make_cert(names=names, ips=ips)
	return pem(cert), key_pem(key)


def files():
	"""The certs folder's files, name to bytes ({} when there is no folder)."""
	d = runtime.certs_dir()
	return {p.name: p.read_bytes() for p in d.iterdir()} if d.exists() else {}


def audits(session_scope, action):
	"""The details of the audit rows with this action."""
	with session_scope() as s:
		return [a.detail for a in s.query(AuditLog).filter_by(action=action)]


@pytest.fixture
def hostname(app):
	app.backend.settings.update({"public_hostname": "nr01.corp.local"}, None)


# ── upload ──

def test_an_organisation_certificate_is_used(admin, app, client_for, proxy,
                                             hostname, session_scope):
	"""Uploading a wildcard certificate over a self-signed one: 200, the folder holds
	exactly the uploaded pair (marker and old names gone), the page's status and the
	audit name it, and the audit has only the certificate's details, never the key."""
	certs.selfsigned("nr01.corp.local", ["10.1.1.5"], runtime.certs_dir())
	(runtime.certs_dir() / pc.OLD_NAMES_FILE).write_text('{"old.lab": 9999999999}')
	cert_bytes, key_bytes = org_pair(("*.corp.local",))
	resp = upload(client_for(admin), cert_bytes, key_bytes)
	assert resp.status_code == 200
	assert files() == {certs.CERT_FILE: cert_bytes, certs.KEY_FILE: key_bytes}
	# never reissued by NetRollout from now on
	assert not certs.is_selfsigned(runtime.certs_dir())
	assert resp.json["proxy"] == {"state": "not_managed"}
	assert resp.json["access"]["certificate"]["names"] == ["*.corp.local"]
	(detail,) = audits(session_scope, "server.certificate_uploaded")
	assert detail["names"] == ["*.corp.local"] and detail["nginx"] == "not_managed"
	# what was used, never the key
	assert set(detail) == {"subject", "names", "not_after", "warnings", "nginx"}
	assert "PRIVATE KEY" not in json.dumps(detail)


@pytest.mark.parametrize("names, ips", [
	(("other.corp.local",), ()),             # another name
	(("*.local",), ()),                      # a wildcard covers one label only
	(("*.other.local",), ()),                # a wildcard of another domain
	(("nr01.corp.local.other",), ()),        # the name as a prefix only
	((), ("10.1.1.5",)),                     # the server's IP, not its name
])
def test_a_certificate_for_another_name_is_refused(admin, app, client_for, proxy,
                                                   hostname, names, ips):
	"""A certificate that doesn't cover the saved hostname (by browsers' rules) is
	refused with 422 saying so and naming what it covers, and the certificate files
	stay as they were."""
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	before = files()
	resp = upload(client_for(admin), *org_pair(names, ips))
	covers = ", ".join([*names, *ips])
	assert resp.status_code == 422
	assert f"doesn't cover nr01.corp.local (it covers: {covers})" in resp.json["message"]
	assert files() == before


def test_a_certificate_without_alternative_names_is_refused(admin, app, client_for, proxy,
                                                            hostname):
	"""A certificate with no subject alternative names (only a common name, which
	browsers ignore) is refused with 422 saying so, and the certificate files stay as
	they were."""
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	before = files()
	cert, key = make_cert(names=())
	resp = upload(client_for(admin), pem(cert), key_pem(key))
	assert resp.status_code == 422
	assert "has no subject alternative names" in resp.json["message"]
	assert files() == before


def test_a_key_of_another_certificate_is_refused(admin, app, client_for, proxy,
                                                 hostname):
	"""A key that isn't the certificate's is refused with 422 ("doesn't belong"), and
	the certificate files stay as they were."""
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	before = files()
	cert_bytes, _ = org_pair()
	_, other_key = org_pair()
	resp = upload(client_for(admin), cert_bytes, other_key)
	assert resp.status_code == 422 and "doesn't belong" in resp.json["message"]
	assert files() == before


def test_a_password_protected_key_is_refused(admin, app, client_for, proxy,
                                             hostname):
	"""A password-protected key is refused with 422, and nothing is written."""
	cert, key = make_cert(names=("nr01.corp.local",))
	resp = upload(client_for(admin), pem(cert), key_pem(key, b"secret"))
	assert resp.status_code == 422 and "password-protected" in resp.json["message"]
	assert files() == {}


def test_both_files_are_needed_and_small(admin, client_for, proxy):
	"""Only a certificate (no key) is 400 "both files"; a 300 KB file is 400 "too
	big"; nothing is written."""
	client = client_for(admin)
	only_cert = client.post("/admin/server/certificate",
	                        data={"certificate": (io.BytesIO(b"x"), "c.pem")},
	                        content_type="multipart/form-data")
	assert only_cert.status_code == 400 and "both files" in only_cert.json["message"]
	huge = upload(client, b"x" * (300 * 1024), b"y")
	assert huge.status_code == 400 and "too big" in huge.json["message"]
	assert files() == {}


def test_nginx_rejecting_it_puts_the_previous_one_back(admin, app, client_for,
                                                       proxy, hostname):
	"""When nginx rejects the uploaded certificate, the answer is 422 with nginx's
	message and the previous self-signed files (marker included) are back."""
	certs.selfsigned("nr01.corp.local", ["10.1.1.5"], runtime.certs_dir())
	before = files()
	proxy.managed()
	proxy.verdict("rejected", "nginx: [emerg] SSL_CTX_use_PrivateKey failed")
	resp = upload(client_for(admin), *org_pair())
	assert resp.status_code == 422
	assert "nginx rejected the certificate: nginx: [emerg] SSL_CTX_use_PrivateKey" \
	       in resp.json["message"]
	assert files() == before                       # the self-signed one, marker too


# ── generate self-signed ──

def test_a_self_signed_one_replaces_the_organisations(admin, app, client_for,
                                                      proxy, hostname,
                                                      session_scope):
	"""Generate self-signed over an organisation's certificate: 200, a self-signed
	certificate for the saved hostname keeping the old one's IP, marked self-signed,
	and audited with its names."""
	cert_bytes, key_bytes = org_pair(("nr01.corp.local",), ips=("10.1.1.5",))
	upload(client_for(admin), cert_bytes, key_bytes)
	resp = client_for(admin).post("/admin/server/certificate/selfsigned")
	assert resp.status_code == 200
	dns, ips = certs.names_in((runtime.certs_dir() / certs.CERT_FILE).read_bytes())
	assert dns == ["nr01.corp.local"] and [str(i) for i in ips] == ["10.1.1.5"]
	assert certs.is_selfsigned(runtime.certs_dir())
	assert resp.json["access"]["certificate"]["selfsigned"]
	(detail,) = audits(session_scope, "server.certificate_generated")
	assert detail["names"] == ["nr01.corp.local", "10.1.1.5"]


def test_generating_drops_old_names_in_transition(admin, app, client_for, proxy,
                                                  hostname):
	"""Generate self-signed names only the saved hostname, dropping an old name in
	transition, and removes the old-names file."""
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir(),
	                 also_names=["old.lab"])
	(runtime.certs_dir() / pc.OLD_NAMES_FILE).write_text('{"old.lab": 9999999999}')
	assert client_for(admin).post("/admin/server/certificate/selfsigned").status_code == 200
	assert certs.names_in((runtime.certs_dir() / certs.CERT_FILE).read_bytes())[0] \
	       == ["nr01.corp.local"]
	assert not (runtime.certs_dir() / pc.OLD_NAMES_FILE).exists()


def test_without_a_hostname_the_current_name_is_kept(admin, app, client_for,
                                                     proxy):
	"""With no hostname saved, Generate self-signed keeps the current certificate's
	name."""
	certs.selfsigned("box.lab", ["10.1.1.5"], runtime.certs_dir())
	assert client_for(admin).post("/admin/server/certificate/selfsigned").status_code == 200
	assert certs.names_in((runtime.certs_dir() / certs.CERT_FILE).read_bytes())[0] \
	       == ["box.lab"]


def test_without_any_name_it_asks_for_the_hostname(admin, client_for, proxy):
	"""With no hostname and no certificate, Generate is 422 "Set the hostname first"
	and writes nothing."""
	resp = client_for(admin).post("/admin/server/certificate/selfsigned")
	assert resp.status_code == 422 and "Set the hostname first" in resp.json["message"]
	assert files() == {}


def test_a_generate_nginx_rejects_puts_the_previous_one_back(admin, app,
                                                             client_for, proxy,
                                                             hostname):
	"""When nginx rejects the generated certificate, the answer is 422 and the previous
	files are back."""
	upload(client_for(admin), *org_pair())
	before = files()
	proxy.managed()
	proxy.verdict("rejected", "nginx: [emerg] bad")
	resp = client_for(admin).post("/admin/server/certificate/selfsigned")
	assert resp.status_code == 422 and files() == before


def test_a_generate_that_cannot_write_changes_nothing(admin, app, client_for,
                                                      proxy, hostname,
                                                      monkeypatch):
	"""A generate that fails half-way (files written, then PermissionError) is 422 with
	the error, and the previous files are back."""
	upload(client_for(admin), *org_pair())
	before = files()
	real = certs.selfsigned

	def half_written(*args, **kwargs):          # the key lands, then it fails
		real(*args, **kwargs)
		raise PermissionError(13, "Permission denied",
		                      str(runtime.certs_dir() / certs.CERT_FILE))
	monkeypatch.setattr(certs, "selfsigned", half_written)
	resp = client_for(admin).post("/admin/server/certificate/selfsigned")
	assert resp.status_code == 422 and "Permission denied" in resp.json["message"]
	assert files() == before


# ── who ──

def test_only_admins_change_the_certificate(make_user, client_for, proxy):
	"""An operator's upload and generate are both refused (302 or 403), and nothing is
	written."""
	client = client_for(make_user(role="operator"))
	assert upload(client, *org_pair()).status_code in (302, 403)
	assert client.post("/admin/server/certificate/selfsigned").status_code in (302, 403)
	assert files() == {}


def test_the_page_shows_the_certificate_card(admin, client_for, proxy):
	"""The Server Management page shows the certificate status card with the
	certificate's name."""
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	page = client_for(admin).get("/admin/server").data
	assert b'id="certStatus"' in page and b"nr01.corp.local" in page


def test_generate_covers_the_servers_ips(admin, app, client_for, proxy, hostname,
                                         monkeypatch):
	"""Generating over an organisation's certificate (usually no IP SANs) puts the
	server's addresses from NETROLLOUT_SERVER_IPS (recorded by the installer) in
	the new one."""
	upload(client_for(admin), *org_pair())
	monkeypatch.setenv(pc.SERVER_IPS_ENV, "10.9.9.9")
	assert client_for(admin).post("/admin/server/certificate/selfsigned").status_code == 200
	_, ips = certs.names_in((runtime.certs_dir() / certs.CERT_FILE).read_bytes())
	assert [str(i) for i in ips] == ["10.9.9.9"]
