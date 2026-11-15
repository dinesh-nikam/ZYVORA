from __future__ import annotations

import base64
import logging
import os
import time
import uuid

import jwt
import pytest

from app.core.config import Settings
from app.core.crypto import BlindIndexer, EncryptionError, FieldCipher, generate_field_key
from app.core.errors import AuthenticationFailed, ValidationFailed
from app.core.logging import RedactingFilter, redact
from app.core.security import (
    TokenService,
    hash_password,
    looks_like_api_key,
    new_api_key,
    validate_password_policy,
    verify_password,
)
from tests.conftest import make_settings


def test_argon2id_hashing() -> None:
    hashed = hash_password("Correct-Horse-Battery-9")
    assert hashed.startswith("$argon2id$")
    assert verify_password("Correct-Horse-Battery-9", hashed)
    assert not verify_password("wrong-password-xyz", hashed)
    assert not verify_password("anything", None)


@pytest.mark.parametrize("pwd", ["short", "password1234", "aaaaaaaaaaaaaaaa", "analystjohn-secret"])
def test_password_policy(pwd: str) -> None:
    with pytest.raises(ValidationFailed):
        validate_password_policy(pwd, email="analystjohn@example.org", min_length=12)


def test_jwt_roundtrip_and_tampering(tmp_path) -> None:
    tokens = TokenService(make_settings(tmp_path))
    uid = uuid.uuid4()
    token, claims = tokens.issue_access_token(uid, "analyst")
    assert tokens.decode_access_token(token).subject == uid
    header, payload, sig = token.split(".")
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(f"{header}.{payload}.{sig[:-2]}AA")


def test_jwt_alg_none_rejected(tmp_path) -> None:
    tokens = TokenService(make_settings(tmp_path))
    forged = jwt.encode({"sub": str(uuid.uuid4()), "typ": "access", "jti": "x", "iat": int(time.time()),
                         "exp": int(time.time()) + 60, "aud": "phoneintel-api", "iss": "phoneintel"},
                        key="", algorithm="none")
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(forged)


def test_jwt_wrong_secret_audience_and_expiry(tmp_path) -> None:
    settings = make_settings(tmp_path)
    tokens = TokenService(settings)
    base = {"sub": str(uuid.uuid4()), "typ": "access", "jti": "x", "iat": int(time.time()),
            "iss": "phoneintel", "aud": "phoneintel-api"}
    secret = settings.secret_key.get_secret_value()
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(jwt.encode({**base, "exp": int(time.time()) + 60}, "other-secret-" * 4))
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(jwt.encode({**base, "aud": "other", "exp": int(time.time()) + 60}, secret))
    with pytest.raises(AuthenticationFailed) as exc:
        tokens.decode_access_token(jwt.encode({**base, "exp": int(time.time()) - 60}, secret))
    assert exc.value.code == "token_expired"
    with pytest.raises(AuthenticationFailed):
        tokens.decode_access_token(jwt.encode({**base, "typ": "refresh", "exp": int(time.time()) + 60}, secret))


def test_api_key_format() -> None:
    key, prefix, digest = new_api_key()
    assert looks_like_api_key(key)
    assert key.split("_")[1] == prefix
    assert len(digest) == 64 and key not in digest


def test_field_encryption_roundtrip_and_rotation() -> None:
    old = generate_field_key("k1")
    new = generate_field_key("k2")
    c_old = FieldCipher([old])
    token = c_old.encrypt("+919876543210")
    assert "+919876543210" not in token
    rotated = FieldCipher([new, old])
    assert rotated.decrypt(token) == "+919876543210"
    assert rotated.encrypt("x").split(".")[1] == "k2"
    with pytest.raises(EncryptionError):
        FieldCipher([new]).decrypt(token)


def test_ciphertext_tampering_detected() -> None:
    cipher = FieldCipher([generate_field_key()])
    token = cipher.encrypt("secret")
    version, kid, payload = token.split(".")
    raw = bytearray(base64.urlsafe_b64decode(payload))
    raw[-1] ^= 1
    with pytest.raises(EncryptionError):
        cipher.decrypt(f"{version}.{kid}.{base64.urlsafe_b64encode(bytes(raw)).decode()}")


def test_blind_index_is_keyed_and_purpose_bound() -> None:
    a = BlindIndexer("k" * 32)
    b = BlindIndexer("z" * 32)
    assert a.digest("+919876543210", purpose="phone") == a.digest("+919876543210", purpose="phone")
    assert a.digest("+919876543210", purpose="phone") != b.digest("+919876543210", purpose="phone")
    assert a.digest("1", purpose="phone") != a.digest("1", purpose="telegram_user")


def test_secrets_never_in_settings_repr(tmp_path) -> None:
    settings = make_settings(tmp_path, brave_api_key="brave-super-secret", telegram_bot_token="123456:ABCDEF")
    rendered = repr(settings) + str(settings.model_dump())
    for secret in ("brave-super-secret", "123456:ABCDEF", settings.secret_key.get_secret_value()):
        assert secret not in rendered


@pytest.mark.parametrize("env", ["production", "staging"])
def test_production_refuses_insecure_defaults(env: str) -> None:
    with pytest.raises(ValueError, match="Insecure configuration"):
        Settings(app_env=env, _env_file=None)


def test_production_accepts_hardened_config() -> None:
    settings = Settings(
        app_env="production", _env_file=None, secret_key=os.urandom(32).hex(), blind_index_key=os.urandom(32).hex(),
        field_encryption_keys=generate_field_key(), cors_allowed_origins="https://admin.example.org",
        trusted_hosts="api.example.org", force_https=True, enable_docs=False,
        database_url="postgresql+asyncpg://u:p@db/phoneintel", redis_url="redis://redis:6379/0",
    )
    assert settings.is_production_like


def test_log_redaction() -> None:
    message = ("login token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc password=hunter2 "
               "key pi_abcdef12_" + "A" * 40 + " number +91 98765 43210 bot123456789:" + "B" * 35)
    cleaned = redact(message)
    assert "hunter2" not in cleaned
    assert "A" * 40 not in cleaned
    assert "98765 43210" not in cleaned
    assert "B" * 35 not in cleaned
    record = logging.makeLogRecord({"msg": "calling %s", "args": ("+919876543210",)})
    RedactingFilter().filter(record)
    assert "9876543210" not in record.getMessage()
