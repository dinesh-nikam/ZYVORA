"""Bot configuration (environment variables / .env).

Required: TELEGRAM_BOT_TOKEN, BOT_SERVICE_API_KEY (an API key of a PHONEINTEL
service account holding ``telegram:act_as``), PHONEINTEL_API_URL.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)

    bot_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("bot_token", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN"),
    )
    service_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("service_api_key", "BOT_SERVICE_API_KEY", "TELEGRAM_SERVICE_API_KEY"),
    )
    api_url: str = Field(
        default="http://localhost:8000", validation_alias=AliasChoices("api_url", "PHONEINTEL_API_URL", "API_URL")
    )
    redis_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("redis_url", "BOT_REDIS_URL"),
        description="FSM storage + rate limiting across restarts/replicas",
    )
    allowed_user_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        validation_alias=AliasChoices("allowed_user_ids", "BOT_ALLOWED_USER_IDS"),
        description="Optional allowlist of Telegram user ids (comma separated); empty = any linked user",
    )
    rate_limit_per_minute: int = Field(
        default=20, ge=1, le=600, validation_alias=AliasChoices("rate_limit_per_minute", "BOT_RATE_LIMIT_PER_MINUTE")
    )
    poll_interval_seconds: float = Field(
        default=3.0, gt=0, validation_alias=AliasChoices("poll_interval_seconds", "BOT_POLL_INTERVAL_SECONDS")
    )
    discovery_timeout_seconds: float = Field(
        default=900.0, gt=0, validation_alias=AliasChoices("discovery_timeout_seconds", "BOT_DISCOVERY_TIMEOUT_SECONDS")
    )
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    log_level: str = Field(default="INFO", validation_alias=AliasChoices("log_level", "BOT_LOG_LEVEL"))

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(v) for v in value.replace(" ", "").split(",") if v]
        return value

    @field_validator("api_url")
    @classmethod
    def _url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("PHONEINTEL_API_URL must be an http(s) URL")
        return value.rstrip("/")
