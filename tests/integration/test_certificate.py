"""Server Management → TLS Certificate: an organisation's certificate
uploaded, or a self-signed one generated — checked first, applied all or
nothing (nginx's verdict included), audited."""
import io
import json

import pytest

from src import certs, runtime
from src.db.tables import AuditLog
from src.webapp import proxy_config as pc
from tests.integration.test_admin_settings import admin, proxy  # noqa: F401 — fixtures
from tests.unit.test_certs import key_pem, make_cert, pem

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


def upload(client, cert_bytes, key_bytes):
	data = {"certificate": (io.BytesIO(cert_bytes), "fullchain.pem"),
	        "key": (io.BytesIO(key_bytes), "privkey.pem")}
	return client.post("/admin/server/certificate", data=data,
	                   content_type="multipart/form-data")


def org_pair(names=("nr01.corp.local",), ips=()):
	cert, key = make_cert(names=names, ips=ips)
	return pem(cert), key_pem(key)


def files():
	d = runtime.certs_dir()
	return {p.name: p.read_bytes() for p in d.iterdir()} if d.exists() else {}


def audits(session_scope, action):
	with session_scope() as s:
		return [a.detail for a in s.query(AuditLog).filter_by(action=action)]


@pytest.fixture
def hostname(app):
	app.backend.settings.update({"public_hostname": "nr01.corp.local"}, None)


# ── upload ──

def test_an_organisation_certificate_is_used(admin, app, client_for, proxy,
                                             hostname, session_scope):
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


@pytest.mark.parametrize("names, expected", [
	(("other.corp.local",), "doesn't cover nr01.corp.local"),
])
def test_a_certificate_for_another_name_is_refused(admin, app, client_for, proxy,
                                                   hostname, names, expected):
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	before = files()
	resp = upload(client_for(admin), *org_pair(names))
	assert resp.status_code == 422 and expected in resp.json["message"]
	assert files() == before


def test_a_key_of_another_certificate_is_refused(admin, app, client_for, proxy,
                                                 hostname):
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	before = files()
	cert_bytes, _ = org_pair()
	_, other_key = org_pair()
	resp = upload(client_for(admin), cert_bytes, other_key)
	assert resp.status_code == 422 and "doesn't belong" in resp.json["message"]
	assert files() == before


def test_a_password_protected_key_is_refused(admin, app, client_for, proxy,
                                             hostname):
	cert, key = make_cert(names=("nr01.corp.local",))
	resp = upload(client_for(admin), pem(cert), key_pem(key, b"secret"))
	assert resp.status_code == 422 and "password-protected" in resp.json["message"]
	assert files() == {}


def test_both_files_are_needed_and_small(admin, client_for, proxy):
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
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir(),
	                 also_names=["old.lab"])
	(runtime.certs_dir() / pc.OLD_NAMES_FILE).write_text('{"old.lab": 9999999999}')
	assert client_for(admin).post("/admin/server/certificate/selfsigned").status_code == 200
	assert certs.names_in((runtime.certs_dir() / certs.CERT_FILE).read_bytes())[0] \
	       == ["nr01.corp.local"]
	assert not (runtime.certs_dir() / pc.OLD_NAMES_FILE).exists()


def test_without_a_hostname_the_current_name_is_kept(admin, app, client_for,
                                                     proxy):
	certs.selfsigned("box.lab", ["10.1.1.5"], runtime.certs_dir())
	assert client_for(admin).post("/admin/server/certificate/selfsigned").status_code == 200
	assert certs.names_in((runtime.certs_dir() / certs.CERT_FILE).read_bytes())[0] \
	       == ["box.lab"]


def test_without_any_name_it_asks_for_the_hostname(admin, client_for, proxy):
	resp = client_for(admin).post("/admin/server/certificate/selfsigned")
	assert resp.status_code == 422 and "Set the hostname first" in resp.json["message"]
	assert files() == {}


def test_a_generate_nginx_rejects_puts_the_previous_one_back(admin, app,
                                                             client_for, proxy,
                                                             hostname):
	upload(client_for(admin), *org_pair())
	before = files()
	proxy.managed()
	proxy.verdict("rejected", "nginx: [emerg] bad")
	resp = client_for(admin).post("/admin/server/certificate/selfsigned")
	assert resp.status_code == 422 and files() == before


def test_a_generate_that_cannot_write_changes_nothing(admin, app, client_for,
                                                      proxy, hostname,
                                                      monkeypatch):
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
	client = client_for(make_user(role="operator"))
	assert upload(client, *org_pair()).status_code in (302, 403)
	assert client.post("/admin/server/certificate/selfsigned").status_code in (302, 403)
	assert files() == {}


def test_the_page_shows_the_certificate_card(admin, client_for, proxy):
	certs.selfsigned("nr01.corp.local", [], runtime.certs_dir())
	page = client_for(admin).get("/admin/server").data
	assert b'id="certStatus"' in page and b"nr01.corp.local" in page


def test_generate_covers_the_servers_ips(admin, app, client_for, proxy, hostname,
                                         monkeypatch):
	# an organisation's certificate usually has no IP SANs: the server's
	# addresses (recorded by the installer) still end up in the new one
	upload(client_for(admin), *org_pair())
	monkeypatch.setenv(pc.SERVER_IPS_ENV, "10.9.9.9")
	assert client_for(admin).post("/admin/server/certificate/selfsigned").status_code == 200
	_, ips = certs.names_in((runtime.certs_dir() / certs.CERT_FILE).read_bytes())
	assert [str(i) for i in ips] == ["10.9.9.9"]
