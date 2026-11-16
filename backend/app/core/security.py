"""Credential primitives: Argon2id password hashing, JWT access tokens,
opaque refresh tokens and API keys.

Plaintext passwords, refresh tokens and API keys are never stored: passwords
are Argon2id hashed; refresh tokens and API keys are high-entropy random
strings stored as SHA-256 digests.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.crypto import sha256_hex
from app.core.errors import AuthenticationFailed, ValidationFailed

# Argon2id with OWASP-recommended parameters (m=19 MiB, t=2, p=1).
_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)
# Pre-computed hash used to equalise timing when the user does not exist.
_DUMMY_HASH = _hasher.hash("phoneintel-dummy-password-for-timing")

_COMMON_PASSWORDS = frozenset(
    {
        "password1234",
        "123456789012",
        "qwertyuiopas",
        "passwordpassword",
        "letmein12345",
        "iloveyou1234",
        "administrator",
        "welcome12345",
        "changeme1234",
        "phoneintel123",
    }
)

API_KEY_PREFIX = "pi"


def utcnow() -> datetime:
    return datetime.now(UTC)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time verification; always performs one Argon2 verify."""
    target = password_hash or _DUMMY_HASH
    try:
        ok = _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return ok and password_hash is not None


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_policy(password: str, *, email: str, min_length: int) -> None:
    problems: list[str] = []
    if len(password) < min_length:
        problems.append(f"Password must be at least {min_length} characters")
    if len(password) > 128:
        problems.append("Password must be at most 128 characters")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS:
        problems.append("Password is too common")
    local_part = email.split("@", 1)[0].lower()
    if len(local_part) >= 4 and local_part in lowered:
        problems.append("Password must not contain the email address")
    if len(set(password)) < 6:
        problems.append("Password must contain more distinct characters")
    if problems:
        raise ValidationFailed("Password does not meet policy", details=problems)


# --------------------------------------------------------------------------
# JWT access tokens
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    subject: uuid.UUID
    jti: str
    expires_at: datetime
    role: str


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.secret_key.get_secret_value()
        self._alg = settings.jwt_algorithm
        if self._alg not in {"HS256", "HS384", "HS512"}:
            raise ValueError("Only HMAC JWT algorithms are supported")
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience
        self._access_ttl = timedelta(minutes=settings.access_token_ttl_minutes)
        self.refresh_ttl = timedelta(days=settings.refresh_token_ttl_days)

    def issue_access_token(self, user_id: uuid.UUID, role: str) -> tuple[str, AccessTokenClaims]:
        now = utcnow()
        exp = now + self._access_ttl
        jti = secrets.token_urlsafe(16)
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "role": role,
            "typ": "access",
            "jti": jti,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "iss": self._issuer,
            "aud": self._audience,
        }
        token = jwt.encode(payload, self._secret, algorithm=self._alg)
        return token, AccessTokenClaims(subject=user_id, jti=jti, expires_at=exp, role=role)

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        try:
            payload = jwt.decode(
                token,
                self._secret,
                algorithms=[self._alg],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "sub", "jti", "typ", "aud", "iss"]},
                leeway=5,
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationFailed("Access token expired", code="token_expired") from exc
        except jwt.PyJWTError as exc:
            raise AuthenticationFailed("Invalid access token", code="invalid_token") from exc
        if payload.get("typ") != "access":
            raise AuthenticationFailed("Invalid token type", code="invalid_token")
        try:
            subject = uuid.UUID(payload["sub"])
        except (ValueError, TypeError) as exc:
            raise AuthenticationFailed("Invalid token subject", code="invalid_token") from exc
        return AccessTokenClaims(
            subject=subject,
            jti=str(payload["jti"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            role=str(payload.get("role", "")),
        )


# --------------------------------------------------------------------------
# Opaque secrets (refresh tokens, API keys, link codes)
# --------------------------------------------------------------------------
def new_refresh_token() -> tuple[str, str]:
    """Return ``(plaintext, sha256_hash)`` for a new refresh token."""
    token = "rt_" + secrets.token_urlsafe(48)
    return token, sha256_hex(token)


def hash_opaque_token(token: str) -> str:
    return sha256_hex(token)


def new_api_key() -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, sha256_hash)``.  Format: ``pi_<prefix>_<secret>``."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    key = f"{API_KEY_PREFIX}_{prefix}_{secret}"
    return key, prefix, sha256_hex(key)


def looks_like_api_key(value: str) -> bool:
    parts = value.split("_", 2)
    return len(parts) == 3 and parts[0] == API_KEY_PREFIX and len(parts[1]) == 8 and len(parts[2]) >= 32


def new_link_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))
