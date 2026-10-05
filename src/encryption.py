import binascii
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.runtime import StartupError, in_container

KEY_DIR = Path.home() / ".netrollout"
KEY_FILE = KEY_DIR / "encryption.key"
ENV_VAR = "NETROLLOUT_ENCRYPTION_KEY"

# Set once at startup by init_encryption() — never at import time, so
# importing this module (tests, CLI) has no side effects
_fernet: Fernet | None = None


class InvalidEncryptionKeyError(Exception):
	"""Raised at request time when stored data can't be decrypted with the
	configured key."""
	pass


class EncryptionStartupError(StartupError):
	"""Raised by init_encryption() when the app must refuse to start: the key
	is malformed, missing while encrypted data exists, or doesn't match the
	stored data."""
	pass


def key_source() -> str:
	"""Where this process reads its key from (for the admin fix-it page).
	Same precedence as read_key: the env var wins over the file."""
	if os.environ.get(ENV_VAR):
		return f"the {ENV_VAR} environment variable"
	return f"the key file {KEY_FILE}"


def read_key() -> bytes | None:
	# env var takes precedence over the key file; None when neither exists
	env_key = os.environ.get(ENV_VAR)
	if env_key:
		return env_key.encode()
	if KEY_FILE.exists():
		return KEY_FILE.read_bytes().strip()
	return None


def _generate_key() -> bytes:
	new_key = Fernet.generate_key()
	KEY_DIR.mkdir(parents=True, exist_ok=True)
	with open(KEY_FILE, "wb") as key_file:
		key_file.write(new_key)
	os.chmod(KEY_FILE, 0o600)
	print(f"[NetRollout] Encryption key generated and saved to {KEY_FILE}")
	print("[NetRollout] Keep this file secure — it protects stored credentials.")
	return new_key


def _build_cipher(raw_key: bytes) -> Fernet:
	# Validate the key format before using it
	try:
		return Fernet(raw_key)
	except (binascii.Error, ValueError) as e:
		try:
			display_str = raw_key.decode("utf-8").strip()
		except (AttributeError, UnicodeDecodeError):
			display_str = str(raw_key)
		safe_preview = f"'{display_str[:4]}...{display_str[-4:]}'" if display_str else "''"
		raise EncryptionStartupError(
			f"The encryption key provided {safe_preview} is invalid: {e}. "
			f"Ensure {ENV_VAR} or {KEY_FILE} contains a valid 32-byte URL-safe base64 string."
		)


def require_key_in_container() -> None:
	"""In a container the key must come from the environment: a key file
	written inside it would vanish with it at the next update, and every
	stored credential with it. Needs no database, so the app checks it before
	touching one. :raises EncryptionStartupError: missing in a container"""
	if in_container() and not os.environ.get(ENV_VAR):
		raise EncryptionStartupError(
			f"{ENV_VAR} is not set. In Docker the encryption key comes from the "
			f"installation's .env (the installer generates it); a key is never "
			f"generated inside a container. Restore it in .env and start again.")


def init_encryption(sample_ciphertext: str | None,
                    db_checked: bool = True) -> None:
	"""Load (or, on a fresh install only, generate) the key and verify it.
	:param sample_ciphertext: one value already stored encrypted in the DB,
	 or None if the DB holds no encrypted data (fresh install)
	:param db_checked: False if the DB was unreachable, so whether encrypted
	 data exists is unknown
	:raises EncryptionStartupError: the app must not start
	"""
	global _fernet
	require_key_in_container()
	raw_key = read_key()
	if raw_key is None:
		if sample_ciphertext is not None:
			# Generating here would silently orphan every stored secret
			raise EncryptionStartupError(
				f"No encryption key found, but the database already holds "
				f"encrypted credentials. A new key cannot decrypt them. Restore "
				f"the original key via {ENV_VAR} or {KEY_FILE}.")
		if not db_checked:
			raise EncryptionStartupError(
				f"No encryption key found and the database is unreachable, so "
				f"a fresh install can't be told apart from a lost key. Refusing "
				f"to generate one. Bring the database up, or restore the key via "
				f"{ENV_VAR} or {KEY_FILE}.")
		raw_key = _generate_key()
	elif not db_checked:
		print("[NetRollout] Database unreachable at startup — encryption key "
		      "loaded but not verified against stored credentials.", flush=True)

	cipher = _build_cipher(raw_key)
	if sample_ciphertext is not None:
		# Canary: catch a wrong key at startup, not at a user's first rollout
		try:
			cipher.decrypt(sample_ciphertext.encode())
		except InvalidToken:
			raise EncryptionStartupError(
				f"The encryption key in {ENV_VAR if os.environ.get(ENV_VAR) else KEY_FILE} "
				f"does not match the credentials stored in the database. Restore "
				f"the key they were encrypted with.")
	_fernet = cipher


def _cipher() -> Fernet:
	if _fernet is None:
		raise RuntimeError("Encryption not initialized — init_encryption() must "
		                   "run at startup")
	return _fernet


def encrypt(plaintext: str) -> str:
	return _cipher().encrypt(plaintext.encode()).decode() if plaintext else ""


def decrypt(ciphertext: str) -> str:
	if not ciphertext:
		return ""
	try:
		# Convert the stored ciphertext string back to bytes and decrypt it
		decrypted_bytes = _cipher().decrypt(ciphertext.encode())
		return decrypted_bytes.decode("utf-8")
	except InvalidToken:
		raise InvalidEncryptionKeyError(
			"Decryption failed. The system encryption key configuration does not match "
			"the credential data stored in the database."
		)
