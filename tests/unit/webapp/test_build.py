"""Building the app (src/webapp/build.py): the secret key - from the
environment, required in a container, random per run in development; the
static files' URLs."""
import pytest
from flask import Flask, render_template_string

from src import runtime
from src.webapp.build import asset, configure_app, resolve_secret_key


# ── SECRET_KEY ───────────────────────────────────────────────────────────────

def test_secret_key_from_the_environment(container):
	"""In a container SECRET_KEY is taken from the environment, spaces trimmed."""
	assert resolve_secret_key({"SECRET_KEY": " s3cret "}) == "s3cret"


def test_missing_secret_key_refuses_in_a_container(container):
	"""In a container a missing SECRET_KEY is a StartupError ("SECRET_KEY is not
	set")."""
	with pytest.raises(runtime.StartupError, match="SECRET_KEY is not set"):
		resolve_secret_key({})


def test_missing_secret_key_is_random_per_run_in_dev(dev, capsys):
	"""In dev a missing SECRET_KEY gets a random 64-character key, different on
	each call and never "dev", and the console says so."""
	first, second = resolve_secret_key({}), resolve_secret_key({})
	assert first != second and len(first) == 64 and first != "dev"
	assert "random key for this run" in capsys.readouterr().out


# ── Static files ─────────────────────────────────────────────────────────────

def test_an_asset_url_carries_the_version():
	"""asset() is the static file's URL with ?v=<VERSION>: after an update the
	browser asks for the new file instead of running its cached old one."""
	with Flask(__name__).test_request_context():
		assert asset("js/inventory.js") == f"/static/js/inventory.js?v={runtime.VERSION}"


def test_templates_can_call_asset():
	"""configure_app makes asset() a template global."""
	app = Flask(__name__)
	configure_app(app, "k")
	with app.test_request_context():
		assert render_template_string("{{ asset('css/x.css') }}") == f"/static/css/x.css?v={runtime.VERSION}"
