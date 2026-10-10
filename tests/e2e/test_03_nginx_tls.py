"""Check 2: nginx and TLS - the installer's certificate, the canonical name,
port 80's redirect, the live log stream unbuffered."""
import re
import socket
import ssl

import pytest

from tests.e2e.harness import HOSTNAME, HTTPS_PORT, Browser, http_get


def peer_certificate(install, server_name: str) -> dict:
	""":returns: the certificate nginx presents, verified against the install's
	 certificate for `server_name` (SNI and the name check)
	:raises ssl.SSLCertVerificationError: it doesn't cover that name"""
	context = ssl.create_default_context(cafile=str(install.certificate))
	with socket.create_connection(("127.0.0.1", HTTPS_PORT), timeout=30) as raw:
		with context.wrap_socket(raw, server_hostname=server_name) as tls:
			return tls.getpeercert()


def test_the_certificate_covers_the_hostname_and_the_server_ip(install):
	for name in (HOSTNAME, "127.0.0.1"):
		cert = peer_certificate(install, name)
	names = set(cert["subjectAltName"])
	assert ("DNS", HOSTNAME) in names
	assert ("IP Address", "127.0.0.1") in names
	# the check above can fail: a name it doesn't cover is refused
	with pytest.raises(ssl.SSLCertVerificationError):
		peer_certificate(install, "other.netrollout.test")


@pytest.mark.parametrize("host", [f"other.netrollout.test:{HTTPS_PORT}",
                                  f"E2E-other.example:{HTTPS_PORT}"])
def test_another_name_is_redirected_to_the_canonical_one(install, host):
	answer = Browser(install, host=host).get("/_netrollout/health?x=1")
	assert answer.status == 301
	assert answer.location == f"https://{HOSTNAME}:{HTTPS_PORT}/_netrollout/health?x=1"


@pytest.mark.parametrize("host", [f"{HOSTNAME}:{HTTPS_PORT}", f"127.0.0.1:{HTTPS_PORT}",
                                  f"localhost:{HTTPS_PORT}"])
def test_the_canonical_name_ips_and_localhost_are_served(install, host):
	answer = Browser(install, host=host).get("/_netrollout/health")
	assert answer.status == 200 and answer.json()["status"] == "ok"


@pytest.mark.parametrize(("host", "to"), [
	(HOSTNAME, HOSTNAME),
	("other.netrollout.test", HOSTNAME),     # another name: the canonical one
	("127.0.0.1:18080", "127.0.0.1"),        # an IP stays the IP
])
def test_port_80_redirects_to_https_on_the_https_port(install, host, to):
	answer = http_get(host, "/results?page=2")
	assert answer.status == 301
	assert answer.location == f"https://{to}:{HTTPS_PORT}/results?page=2"


def test_the_live_log_stream_is_not_buffered(install):
	"""nginx's rendered site: the stream's location turns buffering off (a
	stream through a buffer arrives at its end), the rest keeps it."""
	site = install.exec("nginx", "cat", "/etc/nginx/netrollout/active/site.conf").stdout.decode()
	blocks = dict(re.findall(r"location\s+([^{]+?)\s*\{(.*?)\n    \}", site, flags=re.S))
	stream = blocks["/rollout/stream/"]
	assert re.search(r"^\s*proxy_buffering off;", stream, flags=re.M)
	assert re.search(r"^\s*proxy_cache off;", stream, flags=re.M)
	assert "proxy_buffering" not in blocks["/"]
	# and it is the configuration nginx runs
	effective = install.exec("nginx", "nginx", "-T").stdout.decode()
	assert "location /rollout/stream/" in effective and "proxy_buffering off;" in effective
