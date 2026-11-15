from __future__ import annotations

import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import audit_context, get_container, get_session, require
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import Permission
from app.core.security import utcnow
from app.models.enums import UserStatus
from app.repositories.audit import AuditRepository
from app.repositories.users import UserRepository
from app.schemas.admin import (
    AdminUserOut,
    AdminUserUpdateIn,
    AuditLogOut,
    ComponentHealth,
    SystemHealthOut,
)
from app.schemas.common import ERROR_RESPONSES, PageOut
from app.services import audit
from app.services.auth import Principal
from app.services.container import Container
from app.workers.runner import DLQ_KEY

router = APIRouter(prefix="/admin", tags=["administration"], responses=ERROR_RESPONSES)
VERSION = "1.0.0"


def _user_out(u) -> AdminUserOut:  # type: ignore[no-untyped-def]
    return AdminUserOut(id=u.id, email=u.email, status=u.status, role=u.role.name, created_at=u.created_at,
                        updated_at=u.updated_at, last_login_at=u.last_login_at, locked_until=u.locked_until)


@router.get("/users", response_model=PageOut[AdminUserOut])
async def list_users(
    request: Request,
    principal: Principal = Depends(require(Permission.ADMIN_USERS)),
    session: AsyncSession = Depends(get_session),
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    role: str | None = Query(default=None, max_length=32, pattern=r"^[a-z_]+$"),
    email_contains: str | None = Query(default=None, max_length=100),
    sort: str = Query(default="-created_at", max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> PageOut[AdminUserOut]:
    result = await UserRepository(session).list(status=status_filter.value if status_filter else None, role=role,
                                                email_contains=email_contains, sort=sort, page=page,
                                                page_size=page_size)
    audit.record(session, audit_context(request, principal), "admin.users.list", "user", None)
    await session.commit()
    return PageOut(items=[_user_out(u) for u in result.items], total=result.total, page=page, page_size=page_size)


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdateIn,
    request: Request,
    principal: Principal = Depends(require(Permission.ADMIN_USERS)),
    session: AsyncSession = Depends(get_session),
) -> AdminUserOut:
    """Approve, suspend or change the role of a user.  Admins cannot modify their own account here."""
    repo = UserRepository(session)
    user = await repo.get(user_id)
    if user is None or user.status == UserStatus.DELETED.value:
        raise NotFound("User not found")
    if user.id == principal.id:
        raise ValidationFailed("Administrators cannot change their own status or role", code="self_modification")
    changes: dict[str, str] = {}
    if body.status:
        user.status = body.status
        changes["status"] = body.status
        if body.status == UserStatus.SUSPENDED.value:
            from app.repositories.users import ApiKeyRepository, TokenRepository  # noqa: PLC0415

            await TokenRepository(session).revoke_all_for_user(user.id, utcnow())
            await ApiKeyRepository(session).revoke_all_for_user(user.id, utcnow())
    if body.role:
        role = await repo.role_by_name(body.role)
        if role is None:
            raise ValidationFailed("Unknown role", code="invalid_role")
        user.role_id = role.id
        user.role = role
        changes["role"] = body.role
    audit.record(session, audit_context(request, principal), "admin.user.update", "user", user.id, **changes)
    await session.commit()
    await session.refresh(user)
    return _user_out(user)


@router.get("/audit-logs", response_model=PageOut[AuditLogOut])
async def list_audit_logs(
    request: Request,
    principal: Principal = Depends(require(Permission.ADMIN_AUDIT)),
    session: AsyncSession = Depends(get_session),
    actor_id: uuid.UUID | None = None,
    action: str | None = Query(default=None, max_length=64, pattern=r"^[a-z_.]+$"),
    resource_type: str | None = Query(default=None, max_length=48, pattern=r"^[a-z_]+$"),
    resource_id: str | None = Query(default=None, max_length=64),
    since: datetime | None = None,
    until: datetime | None = None,
    sort: str = Query(default="-timestamp", max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
) -> PageOut[AuditLogOut]:
    result = await AuditRepository(session).list(actor_id=actor_id, action=action, resource_type=resource_type,
                                                 resource_id=resource_id, since=since, until=until, sort=sort,
                                                 page=page, page_size=page_size)
    audit.record(session, audit_context(request, principal), "admin.audit.read", "audit_log", None)
    await session.commit()
    items = [AuditLogOut(id=a.id, actor_id=a.actor_id, action=a.action, resource_type=a.resource_type,
                         resource_id=a.resource_id, timestamp=a.timestamp, ip_address=a.ip_address,
                         metadata=a.meta) for a in result.items]
    return PageOut(items=items, total=result.total, page=page, page_size=page_size)


async def check_components(container: Container) -> dict[str, ComponentHealth]:
    components: dict[str, ComponentHealth] = {}
    started = time.perf_counter()
    try:
        async with container.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        components["database"] = ComponentHealth(status="ok", latency_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:
        components["database"] = ComponentHealth(status="down", detail=type(exc).__name__)
    started = time.perf_counter()
    try:
        await container.redis.ping()
        components["redis"] = ComponentHealth(status="ok", latency_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:
        components["redis"] = ComponentHealth(status="down", detail=type(exc).__name__)
    return components


@router.get("/system-health", response_model=SystemHealthOut)
async def system_health(
    principal: Principal = Depends(require(Permission.ADMIN_HEALTH)),
    container: Container = Depends(get_container),
) -> SystemHealthOut:
    components = await check_components(container)
    dlq = 0
    if components["redis"].status == "ok":
        dlq = int(await container.redis.llen(DLQ_KEY))
    components["search"] = ComponentHealth(
        status="ok" if container.search_providers else "degraded",
        detail=None if container.search_providers else "no public search provider configured",
    )
    overall = "down" if any(c.status == "down" for c in components.values()) else (
        "degraded" if any(c.status == "degraded" for c in components.values()) or dlq else "ok")
    return SystemHealthOut(
        status=overall, environment=container.settings.app_env.value, version=VERSION, components=components,
        search_providers=[p.name for p in container.search_providers],
        platform_api_clients=sorted(container.platform_clients), dead_letter_jobs=dlq, checked_at=utcnow(),
    )
