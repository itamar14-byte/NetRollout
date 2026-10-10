"""End-to-end checks: a whole install, from the release's own files and the
images by tag, started with Docker and checked from outside - through
nginx, as people and the scripts reach it.

Opt-in: skipped unless NETROLLOUT_E2E=1, so a plain `pytest` stays fast and
needs no Docker.

  NETROLLOUT_E2E=1 python -m pytest tests/e2e

  NETROLLOUT_E2E_TAG    the images' tag (default e2e-local):
                        itamarweinstein/netrollout:<tag>, .../netrollout-nginx:<tag>
  NETROLLOUT_E2E_BUILD  1: build both images with that tag first (else they
                        must exist - a missing one fails the run, not skips it)
  NETROLLOUT_E2E_DIR    the scratch install folder (default: a new temp folder)
  NETROLLOUT_E2E_KEEP   1: leave the containers and the folder after the run
                        (debugging; CI prints their logs on a failure)

The install is compose project nre2e, HTTPS on 18443, port 80's redirect on
18080 - never the developer's install or dev stack."""
import os
import tempfile
from pathlib import Path

import pytest

from tests.e2e.harness import (APP_IMAGE, NGINX_IMAGE, PROJECT, ROOT, Browser,
                               ScratchInstall, image_exists, run)

HERE = Path(__file__).resolve().parent
ENABLED = os.environ.get("NETROLLOUT_E2E") == "1"


def pytest_collection_modifyitems(config, items):
	"""Without NETROLLOUT_E2E=1 every test here is skipped (only these: the
	hook sees the whole session's tests)."""
	if ENABLED:
		return
	skip = pytest.mark.skip(reason="end-to-end checks: set NETROLLOUT_E2E=1 "
	                               "(Docker, the images - see tests/e2e/conftest.py)")
	for item in items:
		if HERE in Path(str(item.fspath)).resolve().parents:
			item.add_marker(skip)


@pytest.fixture(scope="session")
def images():
	"""Both images of the tag: built when NETROLLOUT_E2E_BUILD=1, else they
	must be there - a run that checks nothing must not look green."""
	if os.environ.get("NETROLLOUT_E2E_BUILD") == "1":
		run("docker", "build", "-t", APP_IMAGE, str(ROOT), timeout=1800)
		run("docker", "build", "-t", NGINX_IMAGE, str(ROOT / "deploy" / "nginx"),
		    timeout=600)
	missing = [i for i in (APP_IMAGE, NGINX_IMAGE) if not image_exists(i)]
	if missing:
		pytest.fail(f"Missing image(s): {', '.join(missing)} - build them (or set "
		            f"NETROLLOUT_E2E_BUILD=1)", pytrace=False)
	return APP_IMAGE, NGINX_IMAGE


@pytest.fixture(scope="session")
def install(images):
	"""The scratch install, running (every service healthy); stopped with its
	volumes and removed afterwards unless NETROLLOUT_E2E_KEEP=1."""
	given = os.environ.get("NETROLLOUT_E2E_DIR")
	folder = Path(given) if given else Path(tempfile.mkdtemp(prefix="netrollout-e2e-"))
	scratch = ScratchInstall(folder)
	if given:
		scratch.down()
		scratch.remove()
	keep = os.environ.get("NETROLLOUT_E2E_KEEP") == "1"
	try:
		scratch.create()
		scratch.up()
		yield scratch
	finally:
		if keep:
			print(f"\nKept: {folder} (docker compose -p {PROJECT} ... from there)")
		else:
			scratch.down()
			scratch.remove()


@pytest.fixture(scope="session")
def people():
	"""Who the checks made, shared by the session: the factory admin's new
	password once check 6 (or the admin fixture) set it."""
	return {}


@pytest.fixture(scope="session")
def admin(install, people):
	"""The factory admin, signed in with its password changed (by the first
	sign-in check when it ran first)."""
	browser = Browser(install)
	if "admin" not in people:
		answer = browser.sign_in("admin", "admin")
		assert answer.status == 302, answer
		new = "Rollout-check-42"
		assert browser.change_password(new).status == 302
		people["admin"] = new
		return browser
	answer = browser.sign_in("admin", people["admin"])
	assert answer.status == 302 and answer.location.endswith("/dashboard"), answer
	return browser
