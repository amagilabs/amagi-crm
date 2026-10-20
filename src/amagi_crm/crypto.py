"""Field-level encryption at rest, plus keyed hashes for lookups.

Sensitive columns are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) before
they reach the database. ``MultiFernet`` lets you add a new key in front of the
old ones: new writes use the first key, old rows still decrypt, and
``amagi-crm rotate-keys`` rewrites every row under the new key.

Encrypted values can't be searched with SQL, so exact-match lookups (email
dedupe) go through a "blind index": an HMAC of the normalized value under a
separate secret. The database sees the HMAC, never the email.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class DecryptionError(RuntimeError):
    pass


def generate_fernet_key() -> str:
    return Fernet.generate_key().decode("ascii")


def generate_secret() -> str:
    return secrets.token_urlsafe(48)


def derive_key(secret: str, purpose: str) -> bytes:
    """Derive an independent sub-key per purpose so one HMAC can't stand in for another."""
    return hmac.new(secret.encode("utf-8"), purpose.encode("utf-8"), hashlib.sha256).digest()


class FieldCipher:
    def __init__(self, keys: list[str], secret_key: str):
        if not keys:
            raise ValueError("at least one encryption key is required")
        try:
            self._fernet = MultiFernet([Fernet(k.encode("ascii")) for k in keys])
        except (ValueError, TypeError) as exc:
            raise ValueError("AMAGI_CRM_ENCRYPTION_KEYS contains an invalid Fernet key") from exc
        self._primary = Fernet(keys[0].encode("ascii"))
        self._index_key = derive_key(secret_key, "blind-index")
        self.audit_key = derive_key(secret_key, "audit-chain")

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise DecryptionError(
                "Could not decrypt a stored value. The encryption key that wrote it is not in "
                "AMAGI_CRM_ENCRYPTION_KEYS."
            ) from exc

    def is_primary(self, token: str) -> bool:
        """True if ``token`` was written with the current (first) key."""
        try:
            self._primary.decrypt(token.encode("ascii"))
            return True
        except InvalidToken:
            return False

    def blind_index(self, value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.strip().lower()
        return hmac.new(self._index_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


_active: FieldCipher | None = None


def set_cipher(cipher: FieldCipher | None) -> None:
    global _active
    _active = cipher


def get_cipher() -> FieldCipher:
    if _active is None:
        raise RuntimeError("encryption is not configured; call amagi_crm.crypto.set_cipher() first")
    return _active
