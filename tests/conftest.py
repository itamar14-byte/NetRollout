"""Guards shared by every test (unit and integration).

Tests must never touch the developer's real environment: the encryption key
file, the logs/ and config/ directories, or process-wide encryption state.
"""
import os

import pytest

import src.encryption as encryption
from src import runtime


@pytest.fixture(scope="session", autouse=True)
def _isolate_filesystem(tmp_path_factory):
	# Real key lives at ~/.netrollout/encryption.key — point the module at a
	# throwaway dir so no test can read, generate or overwrite it.
	key_dir = tmp_path_factory.mktemp("netrollout_key")
	saved_key = (encryption.KEY_DIR, encryption.KEY_FILE)
	encryption.KEY_DIR = key_dir
	encryption.KEY_FILE = key_dir / "encryption.key"
	# Every NetRollout folder (logs/, config/, certs/) under a throwaway home
	saved_home = os.environ.get(runtime.HOME_ENV)
	os.environ[runtime.HOME_ENV] = str(tmp_path_factory.mktemp("netrollout_home"))
	yield
	encryption.KEY_DIR, encryption.KEY_FILE = saved_key
	if saved_home is None:
		os.environ.pop(runtime.HOME_ENV, None)
	else:
		os.environ[runtime.HOME_ENV] = saved_home


@pytest.fixture(autouse=True)
def _restore_encryption_state():
	# init_encryption() sets module state and tests set the key env var;
	# restore both so tests can't leak keys into each other
	saved_cipher = encryption._fernet
	saved_env = os.environ.get(encryption.ENV_VAR)
	yield
	encryption._fernet = saved_cipher
	if saved_env is None:
		os.environ.pop(encryption.ENV_VAR, None)
	else:
		os.environ[encryption.ENV_VAR] = saved_env
