"""Server Management -> TLS Certificate -> Trusted CA certificates: the page
lists them, Upload adds (checked, audited server.ca_added), Remove takes one
out (audited server.ca_removed). Admins only (the route matrix covers the
refusals)."""
import io

import pytest

from src.access.certs import TrustedCAs
from src.db.tables import AuditLog
from tests.unit.access.test_certs import key_pem, make_cert, pem

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@pytest.fixture
def admin(make_user):
	return make_user(role="admin")


@pytest.fixture(autouse=True)
def no_trusted_cas():
	"""No trusted CA certificates before or after each test."""
	TrustedCAs().path.unlink(missing_ok=True)
	yield
	TrustedCAs().path.unlink(missing_ok=True)


def upload(client, data):
	return client.post("/admin/server/trusted-ca",
	                   data={"certificates": (io.BytesIO(data), "ca.pem")},
	                   content_type="multipart/form-data")


def audited(session_scope, action):
	with session_scope() as s:
		return [(a.object_label, (a.detail or {}).get("fingerprint"))
		        for a in s.query(AuditLog).filter_by(action=action)]


def test_adding_and_removing_a_ca(admin, client_for, session_scope):
	"""Upload adds the certificates (the answer lists them, each addition
	audited); the page shows them; Remove takes one out, audited."""
	root, _ = make_cert(("Corp Root CA",), ca=True)
	client = client_for(admin, xhr=True)
	resp = upload(client, pem(root))
	body = resp.get_json()
	assert resp.status_code == 200
	assert body["message"] == "Trusted 1 certificate - it applies to the next connection."
	(row,) = body["trusted"]
	assert row["subject"] == "CN=Corp Root CA"
	assert audited(session_scope, "server.ca_added") == [("CN=Corp Root CA", row["fingerprint"])]
	assert "CN=Corp Root CA" in client_for(admin).get("/admin/server").get_data(as_text=True)

	again = upload(client, pem(root)).get_json()
	assert again["message"] == "Already trusted - nothing was added."

	resp = client.post(f"/admin/server/trusted-ca/{row['fingerprint']}/remove")
	assert resp.get_json()["trusted"] == []
	assert audited(session_scope, "server.ca_removed") == [("CN=Corp Root CA", row["fingerprint"])]
	assert client.post(f"/admin/server/trusted-ca/{row['fingerprint']}/remove").status_code == 404


def test_a_key_or_no_file_is_refused(admin, client_for, session_scope):
	"""A private key is refused (422, nothing added, nothing audited); no
	file chosen says so."""
	cert, key = make_cert(("Corp Root CA",), ca=True)
	client = client_for(admin, xhr=True)
	resp = upload(client, pem(cert) + key_pem(key))
	assert resp.status_code == 422
	assert resp.get_json()["message"] == ("Not added: That file holds a private key. "
	                                      "Upload certificates only - never a key.")
	assert TrustedCAs().entries() == []
	assert audited(session_scope, "server.ca_added") == []
	resp = client.post("/admin/server/trusted-ca", data={}, content_type="multipart/form-data")
	assert resp.get_json()["message"] == "Choose a PEM file with one or more CA certificates."
