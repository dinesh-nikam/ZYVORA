"""FastAPI dependencies: container, DB session, principal resolution, RBAC
and rate limiting."""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AuthenticationFailed, ValidationFailed
from app.core.permissions import Permission
from app.core.ratelimit import RateLimitRule
from app.core.security import looks_like_api_key
from app.services.audit import AuditContext
from app.services.auth import AuthService, Principal
from app.services.container import Container
from app.services.investigations import InvestigationService


def get_container(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


async def get_session(container: Container = Depends(get_container)) -> AsyncIterator[AsyncSession]:
    async with container.sessionmaker() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


def client_ip(request: Request) -> str | None:
    host = request.client.host if request.client else None
    if host is None:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def get_auth_service(session: AsyncSession = Depends(get_session),
                     container: Container = Depends(get_container)) -> AuthService:
    return AuthService(session, container.settings, container.tokens, container.redis, container.indexer)


def get_investigation_service(session: AsyncSession = Depends(get_session),
                              container: Container = Depends(get_container)) -> InvestigationService:
    return InvestigationService(session, container)


async def _resolve_principal(request: Request, auth: AuthService) -> Principal:
    authorization = request.headers.get("authorization", "")
    api_key = request.headers.get("x-api-key", "")
    if authorization.lower().startswith("bearer "):
        credential = authorization[7:].strip()
        principal = (await auth.authenticate_api_key(credential) if looks_like_api_key(credential)
                     else await auth.authenticate_bearer(credential))
    elif api_key:
        principal = await auth.authenticate_api_key(api_key.strip())
    else:
        raise AuthenticationFailed()
    tg_header = request.headers.get("x-telegram-user-id")
    if tg_header is not None:
        if not principal.has(Permission.TELEGRAM_ACT_AS):
            raise AuthenticationFailed("X-Telegram-User-Id is only accepted from the Telegram service account",
                                       code="invalid_delegation")
        if not tg_header.isdigit() or len(tg_header) > 19:
            raise ValidationFailed("Invalid X-Telegram-User-Id header", code="invalid_telegram_user")
        principal = await auth.act_as_telegram_user(principal, int(tg_header))
    return principal


async def get_principal(request: Request, auth: AuthService = Depends(get_auth_service),
                        container: Container = Depends(get_container)) -> Principal:
    principal = await _resolve_principal(request, auth)
    rule = RateLimitRule.parse(container.settings.rate_limit_default)
    await container.rate_limiter.enforce("principal", str(principal.id), rule)
    request.state.principal_id = str(principal.id)
    return principal


async def get_user_principal(principal: Principal = Depends(get_principal)) -> Principal:
    """A principal acting for a human user (service accounts must delegate)."""
    if principal.has(Permission.TELEGRAM_ACT_AS):
        raise AuthenticationFailed("Service accounts must act on behalf of a linked user",
                                   code="delegation_required")
    return principal


def require(permission: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    async def dependency(principal: Principal = Depends(get_user_principal)) -> Principal:
        principal.require(permission)
        return principal

    return dependency


def audit_context(request: Request, principal: Principal | None = None) -> AuditContext:
    via = principal.via if principal else "api"
    return AuditContext(actor_id=principal.id if principal else None, ip_address=client_ip(request), via=via)


def rate_limit(scope: str, setting: str) -> Callable[..., Awaitable[None]]:
    """Per-user limit for an operation class (e.g. discovery runs per hour)."""

    async def dependency(principal: Principal = Depends(get_principal),
                         container: Container = Depends(get_container)) -> None:
        rule = RateLimitRule.parse(getattr(container.settings, setting))
        await container.rate_limiter.enforce(scope, str(principal.id), rule)

    return dependency


async def ip_rate_limit(request: Request, container: Container = Depends(get_container)) -> None:
    rule = RateLimitRule.parse(container.settings.rate_limit_auth)
    await container.rate_limiter.enforce("auth_ip", client_ip(request) or "unknown", rule)


IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9_\-]{8,128}$"


def idempotency_key(
    key: str | None = Header(default=None, alias="Idempotency-Key", pattern=IDEMPOTENCY_PATTERN),
) -> str | None:
    return key
