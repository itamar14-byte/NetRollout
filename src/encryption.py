import os
from pathlib import Path

from cryptography.fernet import Fernet

KEY_DIR = Path.home() / ".netrollout"
KEY_FILE = KEY_DIR / "encryption.key"
ENV_VAR = "NETROLLOUT_ENCRYPTION_KEY"


def load_key() -> bytes:
    # check env var
    env_key = os.environ.get(ENV_VAR)
    if env_key:
        return env_key.encode()
    # otherwise, check file
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes().strip()
    # generate and save key
    new_key = Fernet.generate_key()
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(new_key)
    print(f"[NetRollout] Encryption key generated and saved to {KEY_FILE}")
    print(f"[NetRollout] Keep this file secure — it protects stored credentials.")
    return new_key


fernet = Fernet(load_key())


def encrypt(plaintext: str) -> str:
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return fernet.decrypt(ciphertext.encode()).decode()
