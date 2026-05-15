from __future__ import annotations

import base64
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .models import CredentialProfile
from .storage import VAULT_FILE, ensure_data_dirs


KDF_ITERATIONS = 390000


class CredentialStoreError(RuntimeError):
    pass


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase:
        raise CredentialStoreError("A master passphrase is required.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def save_credentials(credentials: list[CredentialProfile], passphrase: str) -> None:
    ensure_data_dirs()
    salt = os.urandom(16)
    fernet = Fernet(_derive_key(passphrase, salt))
    payload = json.dumps([profile.__dict__ for profile in credentials], indent=2).encode("utf-8")
    token = fernet.encrypt(payload)
    VAULT_FILE.write_text(
        json.dumps(
            {
                "version": 1,
                "kdf": "PBKDF2HMAC-SHA256",
                "iterations": KDF_ITERATIONS,
                "salt": base64.b64encode(salt).decode("ascii"),
                "token": token.decode("ascii"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_credentials(passphrase: str) -> list[CredentialProfile]:
    if not VAULT_FILE.exists():
        return []
    blob = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
    try:
        salt = base64.b64decode(blob["salt"])
        fernet = Fernet(_derive_key(passphrase, salt))
        decrypted = fernet.decrypt(blob["token"].encode("ascii"))
    except (InvalidToken, KeyError, ValueError) as exc:
        raise CredentialStoreError("Unable to unlock credential vault.") from exc
    records = json.loads(decrypted.decode("utf-8"))
    return [CredentialProfile(**record) for record in records]
