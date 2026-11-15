from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from app.schemas.common import APIModel


class AdminUserOut(APIModel):
    id: uuid.UUID
    email: str
    status: str
    role: str
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None
    locked_until: datetime | None


class AdminUserUpdateIn(APIModel):
    status: Literal["active", "pending", "suspended"] | None = None
    role: Literal["admin", "analyst", "auditor", "service_telegram"] | None = None


class AuditLogOut(APIModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    timestamp: datetime
    ip_address: str | None
    metadata: dict[str, Any]


class ComponentHealth(APIModel):
    status: Literal["ok", "degraded", "down"]
    detail: str | None = None
    latency_ms: int | None = None


class SystemHealthOut(APIModel):
    status: Literal["ok", "degraded", "down"]
    environment: str
    version: str
    components: dict[str, ComponentHealth]
    search_providers: list[str]
    platform_api_clients: list[str]
    dead_letter_jobs: int
    checked_at: datetime
