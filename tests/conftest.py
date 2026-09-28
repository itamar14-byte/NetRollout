"""Guards shared by every test (unit and integration).

Tests must never touch the developer's real environment: the encryption key
file, the logs/ directory, or process-wide encryption state.
"""
import os

import pytest

import src.encryption as encryption
import src.logging_utils as logging_utils


@pytest.fixture(scope="session", autouse=True)
def _isolate_filesystem(tmp_path_factory):
	# Real key lives at ~/.netrollout/encryption.key — point the module at a
	# throwaway dir so no test can read, generate or overwrite it.
	key_dir = tmp_path_factory.mktemp("netrollout_key")
	logs_dir = tmp_path_factory.mktemp("logs")
	saved = (encryption.KEY_DIR, encryption.KEY_FILE, logging_utils.LOGS_DIR)
	encryption.KEY_DIR = key_dir
	encryption.KEY_FILE = key_dir / "encryption.key"
	# RolloutLogger reads LOGS_DIR at construction; keep test logs out of logs/
	logging_utils.LOGS_DIR = str(logs_dir)
	yield
	encryption.KEY_DIR, encryption.KEY_FILE, logging_utils.LOGS_DIR = saved


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
