"""Check 3: live changes (stage 8) - nginx applies a new hostname from
config/nginx/site.env without a restart, and refuses an invalid value or a
certificate whose key doesn't match while the last good site keeps serving.

Last in the run: each check puts the files back and waits for nginx to
apply them again."""
import pytest

from tests.e2e.harness import HOSTNAME, HTTPS_PORT, Browser, run, wait_for

SITE_ENV = "/data/config/nginx/site.env"
KEY = "/data/certs/privkey.pem"
NEW_NAME = "renamed.netrollout.test"


def site_env_with(original: bytes, hostname: str) -> bytes:
	"""site.env as the app writes it (src/access/site_env.py: KEY=value lines),
	the hostname changed and every other key kept."""
	lines = original.decode().splitlines()
	assert any(line.startswith("NETROLLOUT_HOSTNAME=") for line in lines), lines
	return "".join(f"NETROLLOUT_HOSTNAME={hostname}\n" if line.startswith("NETROLLOUT_HOSTNAME=")
	               else f"{line}\n" for line in lines).encode()


def verdict_after(install, before: dict, state: str, words: str) -> dict:
	"""nginx's next verdict: status.json changed since `before` (its times
	aren't compared with this computer's clock) and says `state` + `words`."""
	def new_verdict():
		now = install.nginx_status()
		return now if now != before and now["state"] == state and words in now["message"] \
			else None
	return wait_for(new_verdict, 60, f"nginx: {state} ({words})")


def nginx_started(install) -> str:
	""":returns: when nginx's container started, and how often it restarted"""
	container = install.compose("ps", "-q", "nginx").stdout.decode().strip()
	return run("docker", "inspect", "-f", "{{.State.StartedAt}} {{.RestartCount}}",
	           container).stdout.decode().strip()


def served(install, host: str) -> int:
	return Browser(install, host=host).get("/_netrollout/health").status


@pytest.fixture
def originals(install):
	"""site.env and the key as they were; put back after the check and
	nginx's verdict on them awaited, so the next check starts from the
	installed site."""
	site, key = install.read(SITE_ENV), install.read(KEY)
	assert install.nginx_status()["state"] == "applied"
	yield site, key
	before = install.nginx_status()
	changed = install.read(SITE_ENV) != site or install.read(KEY) != key
	install.write(SITE_ENV, site)
	install.write(KEY, key)
	if changed:
		verdict_after(install, before, "applied", f"hostname={HOSTNAME} ")
	assert served(install, f"{HOSTNAME}:{HTTPS_PORT}") == 200


def test_a_new_hostname_is_applied_without_a_restart(install, originals):
	site, _ = originals
	started = nginx_started(install)
	before = install.nginx_status()
	install.write(SITE_ENV, site_env_with(site, NEW_NAME))
	verdict = verdict_after(install, before, "applied", f"hostname={NEW_NAME} ")
	assert f"https_port={HTTPS_PORT}" in verdict["message"]
	# the new name is served, the old one is now another name. nginx's reload is
	# graceful: for a moment after the verdict an old worker can still take a
	# new connection with the old site - so the new name is awaited, bounded
	wait_for(lambda: served(install, f"{NEW_NAME}:{HTTPS_PORT}") == 200, 15,
	         f"{NEW_NAME} served after the reload", every=0.5)
	old = Browser(install).get("/_netrollout/health")
	assert old.status == 301
	assert old.location == f"https://{NEW_NAME}:{HTTPS_PORT}/_netrollout/health"
	# nginx reloaded, not restarted
	assert nginx_started(install) == started


def test_an_invalid_hostname_is_rejected_and_the_site_keeps_serving(install, originals):
	site, _ = originals
	before = install.nginx_status()
	install.write(SITE_ENV, site_env_with(site, "not_a host!"))
	verdict = verdict_after(install, before, "rejected", "Not a valid hostname")
	assert "not_a host!" in verdict["message"]
	assert served(install, f"{HOSTNAME}:{HTTPS_PORT}") == 200
	other = Browser(install, host=f"other.netrollout.test:{HTTPS_PORT}").get("/")
	assert other.status == 301 and HOSTNAME in other.location   # the previous site


def test_a_key_that_doesnt_match_the_certificate_is_rejected(install, originals):
	_, key = originals
	before = install.nginx_status()
	other_key = install.python(
		"from cryptography.hazmat.primitives import serialization as s; "
		"from cryptography.hazmat.primitives.asymmetric import ec; "
		"print(ec.generate_private_key(ec.SECP256R1()).private_bytes("
		"s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode())")
	assert other_key.startswith("-----BEGIN PRIVATE KEY-----")
	install.write(KEY, f"{other_key}\n".encode())
	verdict = verdict_after(install, before, "rejected", "key values mismatch")
	assert verdict["message"]
	# still serving, with the installed certificate (verified)
	assert served(install, f"{HOSTNAME}:{HTTPS_PORT}") == 200
