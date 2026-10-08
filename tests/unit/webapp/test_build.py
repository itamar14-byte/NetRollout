"""Building the app (src/webapp/build.py): the secret key - from the
environment, required in a container, random per run in development."""
import pytest

from src import runtime
from src.webapp.build import resolve_secret_key


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
