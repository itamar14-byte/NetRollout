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

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def no_key():
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
	return Fernet(key).encrypt(text.encode()).decode()


# ── Startup matrix ───────────────────────────────────────────────────────────

def test_fresh_install_without_key_generates_one(no_key):
	enc.init_encryption(None)
	assert enc.KEY_FILE.exists()
	assert enc.decrypt(enc.encrypt("s3cret")) == "s3cret"


def test_existing_data_with_matching_key_starts(no_key):
	key = Fernet.generate_key()
	set_env_key(key)
	enc.init_encryption(token(key))
	assert enc.decrypt(token(key, "pw")) == "pw"


def test_existing_data_without_key_refuses_and_does_not_generate(no_key):
	with pytest.raises(enc.EncryptionStartupError, match="No encryption key found"):
		enc.init_encryption(token(Fernet.generate_key()))
	assert not enc.KEY_FILE.exists()
	assert enc._fernet is None


def test_existing_data_with_wrong_key_refuses(no_key):
	set_env_key(Fernet.generate_key())
	with pytest.raises(enc.EncryptionStartupError, match="does not match"):
		enc.init_encryption(token(Fernet.generate_key()))
	assert enc._fernet is None


def test_malformed_key_refuses(no_key):
	set_env_key("not-a-valid-fernet-key")
	with pytest.raises(enc.EncryptionStartupError, match="is invalid"):
		enc.init_encryption(None)


def test_db_unreachable_without_key_refuses_to_generate(no_key):
	with pytest.raises(enc.EncryptionStartupError, match="unreachable"):
		enc.init_encryption(None, db_checked=False)
	assert not enc.KEY_FILE.exists()


def test_db_unreachable_with_key_starts_unverified(no_key, capsys):
	set_env_key(Fernet.generate_key())
	enc.init_encryption(None, db_checked=False)
	assert enc._fernet is not None
	assert "not verified" in capsys.readouterr().out


def test_env_var_takes_precedence_over_key_file(no_key):
	file_key, env_key = Fernet.generate_key(), Fernet.generate_key()
	enc.KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
	enc.KEY_FILE.write_bytes(file_key)
	set_env_key(env_key)
	enc.init_encryption(token(env_key))  # would raise if the file key were used


# ── Request-time behaviour ───────────────────────────────────────────────────

def test_use_before_init_is_an_explicit_error(no_key):
	with pytest.raises(RuntimeError, match="not initialized"):
		enc.encrypt("x")


def test_decrypt_with_wrong_key_raises_request_time_error(no_key):
	set_env_key(Fernet.generate_key())
	enc.init_encryption(None)
	with pytest.raises(enc.InvalidEncryptionKeyError):
		enc.decrypt(token(Fernet.generate_key()))


def test_empty_values_pass_through(no_key):
	set_env_key(Fernet.generate_key())
	enc.init_encryption(None)
	assert enc.encrypt("") == ""
	assert enc.decrypt("") == ""


def test_importing_app_modules_has_no_key_side_effect(tmp_path):
	# Before the fail-fast change, importing src.core generated a key file
	env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
	env.pop(enc.ENV_VAR, None)
	result = subprocess.run([sys.executable, "-c", "import src.core"],
	                        cwd=ROOT, env=env, capture_output=True, text=True)
	assert result.returncode == 0, result.stderr
	assert not (tmp_path / ".netrollout").exists()
