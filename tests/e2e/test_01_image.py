"""Check 7: the app image carries the code and nothing of a developer's
machine - no secrets, certificates, logs, settings or git history."""
from tests.e2e.harness import APP_IMAGE, run


def in_image(script: str) -> str:
	return run("docker", "run", "--rm", APP_IMAGE, "sh", "-c", script).stdout.decode()


def test_the_app_folder_holds_only_what_the_dockerfile_copies(images):
	"""/app is exactly the whitelist: the code, the templates, the pins, the
	licence, VERSION and Grafana's setup + dashboards."""
	assert set(in_image("ls -A /app").split()) == {
		"LICENSE", "VERSION", "requirements.txt", "templates", "src", "grafana"}


def test_no_secret_certificate_log_or_git_anywhere_in_the_app_or_its_data(images):
	"""No .env, runtime.env, *.pem, credentials file, .git or docs under /app
	and /data; /data's folders (mounted over at run time) are empty."""
	found = in_image(
		"find /app /data \\( -name .env -o -name runtime.env -o -name '*.pem' "
		"-o -name '*.key' -o -name credentials -o -name .git -o -name docs "
		"-o -name '.env.*' \\) -print")
	assert found.split() == []
	assert in_image("find /data -mindepth 2 -print").split() == []
	assert set(in_image("ls -A /data").split()) == {"logs", "config", "certs"}
