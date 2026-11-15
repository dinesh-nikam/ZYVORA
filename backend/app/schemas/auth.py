from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field

from app.schemas.common import APIModel


class RegisterIn(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class LoginIn(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshIn(APIModel):
    refresh_token: str = Field(min_length=10, max_length=256)


class LogoutIn(APIModel):
    refresh_token: str | None = Field(default=None, max_length=256)
    all_sessions: bool = False


class TokenOut(APIModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class UserOut(APIModel):
    id: uuid.UUID
    email: str
    status: str
    role: str
    created_at: datetime
    last_login_at: datetime | None = None


class MeOut(UserOut):
    permissions: list[str]
    authenticated_via: str


class ApiKeyCreateIn(APIModel):
    name: str = Field(min_length=1, max_length=100, pattern=r"^[\w .\-]+$")
    permissions: list[str] | None = Field(default=None, max_length=32)
    expires_in_days: int | None = Field(default=90, ge=1, le=365)


class ApiKeyOut(APIModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    permissions: list[str]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    api_key: str = Field(description="Shown once. Store it securely; it cannot be retrieved again.")


class TelegramLinkCodeOut(APIModel):
    code: str
    expires_in: int
    instructions: str


class TelegramLinkIn(APIModel):
    code: str = Field(min_length=6, max_length=16, pattern=r"^[A-Za-z0-9]+$")
    telegram_user_id: int = Field(gt=0, lt=2**63)


class TelegramUnlinkIn(APIModel):
    telegram_user_id: int = Field(gt=0, lt=2**63)
