"""Utilities for deriving encryption keys and encrypting chat content.

This module deliberately keeps state small so the pre-shared password is
never persisted to disk. The password is only used in-memory to derive
symmetric keys for the duration of the client session.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# A short, fixed salt keeps setup simple for the two-person chat scenario.
# The password itself remains secret and is never written to disk.
_DEFAULT_SALT = b"cchat-shared-salt"
_ITERATIONS = 390000


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
