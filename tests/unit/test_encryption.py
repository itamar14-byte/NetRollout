"""init_encryption(): fail-fast key handling at startup.

The DB sample is passed in as a string (that's the function's input), so
these tests need no database. KEY_FILE is redirected to a temp dir by
tests/conftest.py — the real key is never touched.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import src.encryption as enc
from src import runtime


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def no_key():
	"""No key anywhere: the env var unset, no key file, encryption not initialised."""
	os.environ.pop(enc.ENV_VAR, None)
	if enc.KEY_FILE.exists():
		enc.KEY_FILE.unlink()
	enc._fernet = None
	yield
	if enc.KEY_FILE.exists():
		enc.KEY_FILE.unlink()


def set_env_key(key: bytes | str):
	os.environ[enc.ENV_VAR] = key.decode() if isinstance(key, bytes) else key


def token(key: bytes, text: str = "stored-secret") -> str:
	"""`text` encrypted with `key`, as a stored credential would be."""
	return Fernet(key).encrypt(text.encode()).decode()


# ── Startup matrix ───────────────────────────────────────────────────────────

def test_fresh_install_without_key_generates_one(no_key):
	"""No key and no stored data: a key file is generated and encryption works."""
	enc.init_encryption(None)
	assert enc.KEY_FILE.exists()
	assert enc.decrypt(enc.encrypt("s3cret")) == "s3cret"


def test_existing_data_with_matching_key_starts(no_key):
	"""Stored data and the key it was encrypted with: starts and decrypts."""
	key = Fernet.generate_key()
	set_env_key(key)
	enc.init_encryption(token(key))
	assert enc.decrypt(token(key, "pw")) == "pw"


def test_existing_data_without_key_refuses_and_does_not_generate(no_key):
	"""Stored data and no key: refused with "No encryption key found", no key
	file generated, encryption left uninitialised."""
	with pytest.raises(enc.EncryptionStartupError, match="No encryption key found"):
		enc.init_encryption(token(Fernet.generate_key()))
	assert not enc.KEY_FILE.exists()
	assert enc._fernet is None


def test_existing_data_with_wrong_key_refuses(no_key):
	"""Stored data and another key: refused with "does not match", encryption
	left uninitialised."""
	set_env_key(Fernet.generate_key())
	with pytest.raises(enc.EncryptionStartupError, match="does not match"):
		enc.init_encryption(token(Fernet.generate_key()))
	assert enc._fernet is None


def test_malformed_key_refuses(no_key):
	"""A key that isn't a valid Fernet key is refused with "is invalid"."""
	set_env_key("not-a-valid-fernet-key")
	with pytest.raises(enc.EncryptionStartupError, match="is invalid"):
		enc.init_encryption(None)


def test_db_unreachable_without_key_refuses_to_generate(no_key):
	"""The database not checked and no key: refused ("unreachable"), no key
	file generated."""
	with pytest.raises(enc.EncryptionStartupError, match="unreachable"):
		enc.init_encryption(None, db_checked=False)
	assert not enc.KEY_FILE.exists()


def test_db_unreachable_with_key_starts_unverified(no_key, capsys):
	"""The database not checked but a key given: starts and prints that the key
	is "not verified"."""
	set_env_key(Fernet.generate_key())
	enc.init_encryption(None, db_checked=False)
	assert enc._fernet is not None
	assert "not verified" in capsys.readouterr().out


def test_env_var_takes_precedence_over_key_file(no_key):
	"""With both a key file and the env var, the env var's key is used (the
	data matches only it; the file key would raise)."""
	file_key, env_key = Fernet.generate_key(), Fernet.generate_key()
	enc.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
	enc.KEY_FILE.write_bytes(file_key)
	set_env_key(env_key)
	enc.init_encryption(token(env_key))  # would raise if the file key were used


# ── Request-time behaviour ───────────────────────────────────────────────────

def test_use_before_init_is_an_explicit_error(no_key):
	"""Encrypting before init_encryption raises RuntimeError "not initialized"."""
	with pytest.raises(RuntimeError, match="not initialized"):
		enc.encrypt("x")


def test_decrypt_with_wrong_key_raises_request_time_error(no_key):
	"""Decrypting a value encrypted with another key raises
	InvalidEncryptionKeyError."""
	set_env_key(Fernet.generate_key())
	enc.init_encryption(None)
	with pytest.raises(enc.InvalidEncryptionKeyError):
		enc.decrypt(token(Fernet.generate_key()))


def test_empty_values_pass_through(no_key):
	"""An empty string encrypts and decrypts to an empty string."""
	set_env_key(Fernet.generate_key())
	enc.init_encryption(None)
	assert enc.encrypt("") == ""
	assert enc.decrypt("") == ""


def test_importing_app_modules_has_no_key_side_effect(tmp_path):
	"""Importing src.rollout.engine (in a fresh Python, no key set) succeeds and creates no
	~/.netrollout key folder. Before the fail-fast change, importing src.rollout.engine
	generated a key file."""
	env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
	env.pop(enc.ENV_VAR, None)
	result = subprocess.run([sys.executable, "-c", "import src.rollout.engine"],
	                        cwd=ROOT, env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert not (tmp_path / ".netrollout").exists()


# ── Encryption key in a container ────────────────────────────────────────────

@pytest.fixture
def no_env_key(monkeypatch):
	"""No encryption key in the environment; removes the dev key file afterwards."""
	monkeypatch.delenv(enc.ENV_VAR, raising=False)
	yield
	if enc.KEY_FILE.exists():
		enc.KEY_FILE.unlink()


def test_container_never_generates_a_key(container, no_env_key):
	"""In a container without a key, init_encryption refuses ("never generated")
	where dev would generate one, and writes no key file."""
	with pytest.raises(enc.EncryptionStartupError, match="never generated"):
		enc.init_encryption(None)          # a fresh install, in dev: generates
	assert not enc.KEY_FILE.exists()


def test_container_ignores_a_key_file(container, no_env_key):
	"""In a container a key file is not used: without the env key the start is
	refused even when the file exists (a file inside the container would vanish
	at the next update)."""
	enc.KEY_DIR.mkdir(parents=True, exist_ok=True)
	enc.KEY_FILE.write_bytes(Fernet.generate_key())
	with pytest.raises(enc.EncryptionStartupError):
		enc.init_encryption(None)


def test_container_uses_the_env_key(container, monkeypatch):
	"""In a container the key from the environment is used: encrypt then decrypt
	gives the text back."""
	monkeypatch.setenv(enc.ENV_VAR, Fernet.generate_key().decode())
	enc.init_encryption(None)
	assert enc.decrypt(enc.encrypt("x")) == "x"


def test_container_key_is_checked_without_a_database(container, no_env_key):
	"""require_key_in_container refuses a missing key without any database (launch_app
	calls it before BackendServices touches Postgres)."""
	with pytest.raises(enc.EncryptionStartupError):
		enc.require_key_in_container()


def test_encryption_error_is_a_startup_error():
	"""EncryptionStartupError is a StartupError, so the entry point's catch gives a
	readable abort."""
	assert issubclass(enc.EncryptionStartupError, runtime.StartupError)
