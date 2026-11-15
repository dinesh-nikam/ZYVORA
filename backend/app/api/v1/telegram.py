"""Telegram account linking.

Flow: the user creates a one-time code (CLI / API) -> sends ``/link CODE`` to the
bot -> the bot (service account with ``telegram:act_as``) calls ``/link``.
Afterwards the bot acts *as that user* by sending ``X-Telegram-User-Id``; every
authorization decision is made against the linked user's own role.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import audit_context, get_auth_service, get_principal, get_user_principal, ip_rate_limit
from app.core.permissions import Permission
from app.schemas.auth import TelegramLinkCodeOut, TelegramLinkIn, TelegramUnlinkIn, UserOut
from app.schemas.common import ERROR_RESPONSES, MessageOut
from app.services.auth import LINK_CODE_TTL_SECONDS, AuthService, Principal

router = APIRouter(prefix="/integrations/telegram", tags=["telegram integration"], responses=ERROR_RESPONSES)


@router.post("/link-code", response_model=TelegramLinkCodeOut, status_code=status.HTTP_201_CREATED)
async def create_link_code(request: Request, principal: Principal = Depends(get_user_principal),
                           auth: AuthService = Depends(get_auth_service)) -> TelegramLinkCodeOut:
    code = await auth.create_telegram_link_code(principal, audit_context(request, principal))
    await auth.session.commit()
    return TelegramLinkCodeOut(code=code, expires_in=LINK_CODE_TTL_SECONDS,
                               instructions=f"Send '/link {code}' to the PHONEINTEL Telegram bot within 10 minutes.")


@router.post("/link", response_model=UserOut, dependencies=[Depends(ip_rate_limit)])
async def link(body: TelegramLinkIn, request: Request, service: Principal = Depends(get_principal),
               auth: AuthService = Depends(get_auth_service)) -> UserOut:
    """Service-account only (``telegram:act_as``)."""
    service.require(Permission.TELEGRAM_ACT_AS)
    user = await auth.link_telegram(service, body.code, body.telegram_user_id, audit_context(request, service))
    await auth.session.commit()
    return UserOut(id=user.id, email=user.email, status=user.status, role=user.role.name,
                   created_at=user.created_at, last_login_at=user.last_login_at)


@router.post("/unlink", response_model=MessageOut)
async def unlink(body: TelegramUnlinkIn, request: Request, service: Principal = Depends(get_principal),
                 auth: AuthService = Depends(get_auth_service)) -> MessageOut:
    """Service-account only (``telegram:act_as``)."""
    service.require(Permission.TELEGRAM_ACT_AS)
    removed = await auth.unlink_telegram(service, body.telegram_user_id, audit_context(request, service))
    await auth.session.commit()
    return MessageOut(message="unlinked" if removed else "no link existed")
