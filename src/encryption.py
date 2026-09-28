import base64
import binascii
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_DIR = Path.home() / ".netrollout"
KEY_FILE = KEY_DIR / "encryption.key"
ENV_VAR = "NETROLLOUT_ENCRYPTION_KEY"


class InvalidEncryptionKeyError(Exception):
	"""Raised when the Fernet master key is corrupted or formatted incorrectly."""
	pass


def load_key() -> bytes:
	# check env var
	env_key = os.environ.get(ENV_VAR)
	if env_key:
		raw_key = env_key.encode()
	# otherwise, check a file
	elif KEY_FILE.exists():
		raw_key = KEY_FILE.read_bytes().strip()
	# generate and save key
	else:
		new_key = Fernet.generate_key()
		KEY_DIR.mkdir(parents=True, exist_ok=True)
		with open(KEY_FILE, "wb") as key_file:
			key_file.write(new_key)
		os.chmod(KEY_FILE, 0o600)
		print(f"[NetRollout] Encryption key generated and saved to {KEY_FILE}")
		print(
			f"[NetRollout] Keep this file secure — it protects stored credentials.")
		raw_key = new_key

	# Validate the key format before returning it
	try:
		Fernet(raw_key)
	except (binascii.Error, ValueError) as e:
		try:
			display_str = raw_key.decode("utf-8").strip()
		except (AttributeError, UnicodeDecodeError):
			display_str = str(raw_key)
		safe_preview = f"'{display_str[:4]}...{display_str[-4:]}'" if display_str else "''"
		raise InvalidEncryptionKeyError(
			f"The encryption key provided {safe_preview} is invalid: {e}. "
			f"Ensure {ENV_VAR} or {KEY_FILE} contains a valid 32-byte URL-safe base64 string."
		)
	return raw_key


fernet = Fernet(load_key())


def encrypt(plaintext: str) -> str:
	return fernet.encrypt(plaintext.encode()).decode() if plaintext else ""


def decrypt(ciphertext: str) -> str:
	if not ciphertext:
		return ""
	try:
		# Convert the stored ciphertext string back to bytes and decrypt it
		decrypted_bytes = fernet.decrypt(ciphertext.encode())
		return decrypted_bytes.decode("utf-8")
	except InvalidToken:
		raise InvalidEncryptionKeyError(
			"Decryption failed. The system encryption key configuration does not match "
			"the credential data stored in the database."
		)


