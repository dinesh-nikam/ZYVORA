"""Field-level encryption and blind indexing.

* ``FieldCipher`` encrypts sensitive column values with AES-256-GCM.  Each
  ciphertext is prefixed with the key id so keys can be rotated: the first
  configured key encrypts, every configured key can decrypt.
* ``BlindIndexer`` produces a keyed HMAC-SHA256 digest so encrypted values
  (e.g. phone numbers) can still be looked up by equality without storing
  plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_VERSION = b"v1"


class EncryptionError(Exception):
    """Raised when a value cannot be encrypted or decrypted."""


class FieldCipher:
    def __init__(self, keys: Sequence[str]) -> None:
        if not keys:
            raise EncryptionError("at least one field encryption key is required")
        self._keys: dict[str, AESGCM] = {}
        self._active_kid: str | None = None
        for raw in keys:
            kid, sep, encoded = raw.partition(":")
            if not sep or not kid or not encoded:
                raise EncryptionError("encryption keys must use the format '<kid>:<base64-key>'")
            if not kid.isalnum() or len(kid) > 16:
                raise EncryptionError("encryption key id must be alphanumeric and <=16 chars")
            try:
                key = base64.urlsafe_b64decode(encoded)
            except ValueError as exc:
                raise EncryptionError("encryption key is not valid base64") from exc
            if len(key) != 32:
                raise EncryptionError("encryption keys must decode to exactly 32 bytes")
            self._keys[kid] = AESGCM(key)
            if self._active_kid is None:
                self._active_kid = kid

    @property
    def active_key_id(self) -> str:
        assert self._active_kid is not None
        return self._active_kid

    def encrypt(self, plaintext: str, *, associated_data: bytes = b"") -> str:
        kid = self.active_key_id
        nonce = os.urandom(_NONCE_BYTES)
        ct = self._keys[kid].encrypt(nonce, plaintext.encode("utf-8"), associated_data or None)
        token = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
        return f"{_VERSION.decode()}.{kid}.{token}"

    def decrypt(self, token: str, *, associated_data: bytes = b"") -> str:
        try:
            version, kid, payload = token.split(".", 2)
        except ValueError as exc:
            raise EncryptionError("malformed ciphertext") from exc
        if version != _VERSION.decode():
            raise EncryptionError("unsupported ciphertext version")
        aead = self._keys.get(kid)
        if aead is None:
            raise EncryptionError("ciphertext was encrypted with an unknown key id")
        raw = base64.urlsafe_b64decode(payload)
        nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        try:
            return aead.decrypt(nonce, ct, associated_data or None).decode("utf-8")
        except Exception as exc:  # cryptography raises InvalidTag
            raise EncryptionError("ciphertext integrity check failed") from exc


class BlindIndexer:
    def __init__(self, key: str) -> None:
        if len(key) < 16:
            raise EncryptionError("blind index key too short")
        self._key = key.encode("utf-8")

    def digest(self, value: str, *, purpose: str) -> str:
        msg = f"{purpose}\x00{value}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_field_key(kid: str = "k1") -> str:
    """Return a new ``kid:base64key`` string suitable for FIELD_ENCRYPTION_KEYS."""
    return f"{kid}:{base64.urlsafe_b64encode(os.urandom(32)).decode('ascii')}"


# Process-wide cipher used by the SQLAlchemy ``EncryptedText`` column type.
_cipher: FieldCipher | None = None


def configure_cipher(cipher: FieldCipher) -> None:
    global _cipher  # noqa: PLW0603 - single process-wide crypto context
    _cipher = cipher


def get_cipher() -> FieldCipher:
    if _cipher is None:
        raise EncryptionError("field cipher has not been configured")
    return _cipher
