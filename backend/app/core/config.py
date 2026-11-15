"""Application configuration.

All settings come from environment variables (optionally a ``.env`` file).
Secrets are typed as ``SecretStr`` so they never appear in ``repr()``/logs.
Production settings are validated at startup and the process refuses to boot
with insecure values.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class JobBackend(StrEnum):
    ARQ = "arq"
    INLINE = "inline"


_INSECURE_SECRET_MARKERS = ("change-me", "changeme", "insecure", "example", "dev-secret")


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- runtime -----------------------------------------------------------
    app_name: str = "PHONEINTEL"
    app_env: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"
    log_json: bool = True
    api_prefix: str = "/api/v1"
    enable_docs: bool = True

    # --- persistence -------------------------------------------------------
    database_url: SecretStr = SecretStr("postgresql+asyncpg://phoneintel:phoneintel@localhost:5432/phoneintel")
    database_pool_size: int = 10
    database_max_overflow: int = 10
    database_echo: bool = False
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    # --- crypto / auth -----------------------------------------------------
    secret_key: SecretStr = SecretStr("dev-secret-change-me-dev-secret-change-me")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "phoneintel"
    jwt_audience: str = "phoneintel-api"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14
    # Comma separated list of urlsafe-base64 encoded 32-byte AES keys, "kid:key".
    # The first key encrypts; all keys decrypt (supports rotation).
    field_encryption_keys: Annotated[list[SecretStr], NoDecode] = Field(
        default_factory=lambda: [SecretStr("k1:" + base64.urlsafe_b64encode(b"\x00" * 32).decode())]
    )
    blind_index_key: SecretStr = SecretStr("dev-blind-index-key-change-me-0123456789")
    password_min_length: int = 12
    max_failed_logins: int = 10
    lockout_minutes: int = 15
    registration_open: bool = True
    default_user_status: str = "active"

    # --- HTTP edge ---------------------------------------------------------
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    trusted_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["*"])
    force_https: bool = False
    max_request_body_bytes: int = 1_048_576
    metrics_enabled: bool = True
    metrics_token: SecretStr | None = None

    # --- rate limits ("<count>/<second|minute|hour|day>") -----------------
    rate_limit_default: str = "300/minute"
    rate_limit_auth: str = "10/minute"
    rate_limit_discovery: str = "20/hour"
    rate_limit_phone: str = "120/minute"
    rate_limit_verify: str = "60/hour"

    # --- retention ---------------------------------------------------------
    retention_investigation_days: int = 90
    retention_evidence_days: int = 30
    retention_audit_days: int = 365
    deleted_tombstone_days: int = 30

    # --- background jobs ---------------------------------------------------
    job_backend: JobBackend = JobBackend.ARQ
    job_max_tries: int = 4
    job_timeout_seconds: int = 300
    job_backoff_base_seconds: float = 5.0
    worker_max_jobs: int = 8
    discovery_max_concurrency: int = 4

    # --- outbound HTTP -----------------------------------------------------
    outbound_timeout_seconds: float = 10.0
    outbound_max_bytes: int = 524_288
    outbound_max_redirects: int = 5
    outbound_user_agent: str = "PhoneIntelBot/1.0 (+https://github.com/phoneintel; public-source verification)"
    respect_robots_txt: bool = True

    # --- public search providers --------------------------------------
    search_providers: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["brave", "google_cse", "searxng"])
    search_max_queries: int = 12
    search_results_per_query: int = 10
    brave_api_key: SecretStr | None = None
    google_cse_api_key: SecretStr | None = None
    google_cse_cx: str | None = None
    searxng_base_url: str | None = None
    searxng_allow_private_network: bool = False

    # --- official platform APIs -----------------------------------------
    github_token: SecretStr | None = None
    youtube_api_key: SecretStr | None = None
    reddit_client_id: SecretStr | None = None
    reddit_client_secret: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None

    # --- carrier lookup provider (optional, licensed) -----------------
    twilio_account_sid: SecretStr | None = None
    twilio_auth_token: SecretStr | None = None

    # --- reporting ---------------------------------------------------------
    pdf_font_path: str | None = None

    # --- observability -----------------------------------------------------
    otel_enabled: bool = False
    otel_service_name: str = "phoneintel-api"

    @field_validator("cors_allowed_origins", "trusted_hosts", "search_providers", mode="before")
    @classmethod
    def _csv_lists(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("field_encryption_keys", mode="before")
    @classmethod
    def _csv_keys(cls, value: object) -> object:
        split = _split_csv(value)
        if isinstance(split, list):
            return [SecretStr(v) if isinstance(v, str) else v for v in split]
        return split

    @field_validator("default_user_status")
    @classmethod
    def _status(cls, value: str) -> str:
        if value not in {"active", "pending"}:
            raise ValueError("default_user_status must be 'active' or 'pending'")
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app_env not in (Environment.PRODUCTION, Environment.STAGING):
            return self
        problems: list[str] = []
        secret = self.secret_key.get_secret_value()
        if len(secret) < 32 or any(m in secret.lower() for m in _INSECURE_SECRET_MARKERS):
            problems.append("SECRET_KEY must be >=32 chars and not a placeholder")
        blind = self.blind_index_key.get_secret_value()
        if len(blind) < 32 or any(m in blind.lower() for m in _INSECURE_SECRET_MARKERS):
            problems.append("BLIND_INDEX_KEY must be >=32 chars and not a placeholder")
        for raw in self.field_encryption_keys:
            _, _, key = raw.get_secret_value().partition(":")
            try:
                decoded = base64.urlsafe_b64decode(key)
            except ValueError:
                decoded = b""
            if len(decoded) != 32 or decoded == b"\x00" * 32:
                problems.append("FIELD_ENCRYPTION_KEYS must contain real 32-byte keys")
                break
        if "*" in self.cors_allowed_origins:
            problems.append("CORS_ALLOWED_ORIGINS must not contain '*'")
        if "*" in self.trusted_hosts:
            problems.append("TRUSTED_HOSTS must list explicit host names")
        if self.job_backend is not JobBackend.ARQ:
            problems.append("JOB_BACKEND must be 'arq' outside development")
        if self.redis_url.get_secret_value().startswith("memory://"):
            problems.append("REDIS_URL=memory:// is only allowed in development/test")
        if self.database_url.get_secret_value().startswith("sqlite"):
            problems.append("SQLite is only allowed in development/test")
        if self.app_env is Environment.PRODUCTION:
            if not self.force_https:
                problems.append("FORCE_HTTPS must be true in production")
            if self.enable_docs:
                problems.append("ENABLE_DOCS must be false in production")
        if problems:
            raise ValueError("Insecure configuration: " + "; ".join(problems))
        return self

    @property
    def is_production_like(self) -> bool:
        return self.app_env in (Environment.PRODUCTION, Environment.STAGING)

    def configured_search_providers(self) -> list[str]:
        """Return provider names that are enabled AND have credentials."""
        ready: list[str] = []
        for name in self.search_providers:
            if (
                (name == "brave" and self.brave_api_key)
                or (name == "google_cse" and self.google_cse_api_key and self.google_cse_cx)
                or (name == "searxng" and self.searxng_base_url)
            ):
                ready.append(name)
        return ready


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
