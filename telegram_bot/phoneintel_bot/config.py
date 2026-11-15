"""Configuration settings for PHONEINTEL Telegram Bot."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr = Field(
        default=SecretStr("0000000000:TEST_TOKEN_PLACEHOLDER_FOR_LOCAL_DEV"),
        description="Telegram Bot API token from @BotFather",
    )
    api_url: str = Field(default="http://localhost:8000", description="Backend API base URL")
    service_api_key: SecretStr = Field(
        default=SecretStr("dev-telegram-service-key"),
        description="API key for the telegram bot service account (with telegram:act_as)",
    )
    request_timeout_seconds: float = Field(default=30.0, description="HTTP request timeout")
    redis_url: str | None = Field(default=None, description="Optional Redis URL for bot state / rate limiting")
