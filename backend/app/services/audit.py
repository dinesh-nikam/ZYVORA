"""Audit logging.  Entries are written in the caller's transaction so the
audited action and its audit record commit (or roll back) together."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog
from app.repositories.audit import AuditRepository

_FORBIDDEN_META_KEYS = frozenset({"phone", "password", "token", "api_key", "secret", "normalized_phone"})


@dataclass(frozen=True, slots=True)
class AuditContext:
    actor_id: uuid.UUID | None
    ip_address: str | None
    via: str = "api"  # api | api_key | telegram | worker | system


def record(
    session: AsyncSession,
    ctx: AuditContext,
    action: str,
    resource_type: str,
    resource_id: object | None = None,
    **meta: Any,
) -> None:
    leaked = _FORBIDDEN_META_KEYS & meta.keys()
    if leaked:
        raise ValueError(f"audit metadata must not contain sensitive keys: {sorted(leaked)}")
    AuditRepository(session).add(AuditLog(
        actor_id=ctx.actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        ip_address=(ctx.ip_address or None),
        meta={"via": ctx.via, **meta},
    ))
