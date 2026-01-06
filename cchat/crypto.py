"""Utilities for deriving encryption keys and encrypting chat content.

This module deliberately keeps state small so the pre-shared password is
never persisted to disk. The password is only used in-memory to derive
symmetric keys for the duration of the client session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# A short, fixed salt keeps setup simple for the two-person chat scenario.
# The password itself remains secret and is never written to disk.
DEFAULT_SALT_TEXT = "cchat-shared-salt"
_DEFAULT_SALT = DEFAULT_SALT_TEXT.encode("utf-8")
_ITERATIONS = 390000
_FINGERPRINT_SUFFIX = b":username-fingerprint"


def derive_key(password: str, salt: bytes | None = None) -> bytes:
    """Derive a Fernet-compatible key from a password.

    Args:
        password: The pre-shared key provided by the user.
        salt: Optional custom salt value; defaults to a shared salt.

    Returns:
        URL-safe base64-encoded key suitable for :class:`Fernet`.
    """

    chosen_salt = salt or _DEFAULT_SALT
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=chosen_salt,
        iterations=_ITERATIONS,
    )
    key = kdf.derive(password.encode("utf-8"))
    return base64.urlsafe_b64encode(key)


def derive_fingerprint_key(password: str, salt: bytes | None = None) -> bytes:
    """Derive a stable key for username fingerprints."""
    chosen_salt = salt or _DEFAULT_SALT
    return derive_key(password, chosen_salt + _FINGERPRINT_SUFFIX)


def username_fingerprint(fingerprint_key: bytes, username: str) -> str:
    """Create a stable, non-reversible fingerprint for a username."""
    digest = hmac.new(fingerprint_key, username.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def hash_unlock_phrase(phrase: str) -> tuple[str, str]:
    """Hash an unlock phrase for storage."""
    salt = os.urandom(16)
    key = derive_key(phrase, salt)
    return base64.b64encode(salt).decode("ascii"), key.decode("ascii")


def verify_unlock_phrase(phrase: str, salt_b64: str, key_b64: str) -> bool:
    """Verify an unlock phrase against stored hash material."""
    salt = base64.b64decode(salt_b64.encode("ascii"))
    derived = derive_key(phrase, salt).decode("ascii")
    return hmac.compare_digest(derived, key_b64)


@dataclass
class CipherBundle:
    """Collection of helpers for encrypting and decrypting messages."""

    fernet: Fernet

    @classmethod
    def from_password(cls, password: str, salt: Optional[bytes] = None) -> "CipherBundle":
        key = derive_key(password, salt)
        return cls(Fernet(key))

    def encrypt_text(self, text: str) -> str:
        return self.fernet.encrypt(text.encode("utf-8")).decode("utf-8")

    def decrypt_text(self, token: str) -> str:
        try:
            return self.fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:  # pragma: no cover - handled at call site
            raise ValueError("Failed to decrypt message; did you enter the correct password?") from exc
