from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import audit_context, get_auth_service, get_principal, ip_rate_limit
from app.schemas.auth import LoginIn, LogoutIn, MeOut, RefreshIn, RegisterIn, TokenOut, UserOut
from app.schemas.common import ERROR_RESPONSES
from app.services.auth import AuthService, Principal, TokenPair

router = APIRouter(prefix="/auth", tags=["authentication"], responses=ERROR_RESPONSES)


def _tokens(pair: TokenPair) -> TokenOut:
    return TokenOut(
        access_token=pair.access_token,
        expires_in=pair.access_expires_in,
        refresh_token=pair.refresh_token,
        refresh_expires_in=pair.refresh_expires_in,
    )


def user_out(user: Any) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        status=user.status,
        role=user.role.name,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.post(
    "/register", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(ip_rate_limit)]
)
async def register(body: RegisterIn, request: Request, auth: AuthService = Depends(get_auth_service)) -> UserOut:
    """Create an account.  Depending on configuration new accounts may need admin approval."""
    user = await auth.register(body.email, body.password, audit_context(request))
    await auth.session.commit()
    return user_out(user)


@router.post("/login", response_model=TokenOut, dependencies=[Depends(ip_rate_limit)])
async def login(body: LoginIn, request: Request, auth: AuthService = Depends(get_auth_service)) -> TokenOut:
    pair = await auth.login(body.email, body.password, audit_context(request))
    await auth.session.commit()
    return _tokens(pair)


@router.post("/refresh", response_model=TokenOut, dependencies=[Depends(ip_rate_limit)])
async def refresh(body: RefreshIn, request: Request, auth: AuthService = Depends(get_auth_service)) -> TokenOut:
    """Rotate a refresh token.  Reusing an already-rotated token revokes the whole token family."""
    pair = await auth.refresh(body.refresh_token, audit_context(request))
    await auth.session.commit()
    return _tokens(pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: LogoutIn,
    request: Request,
    principal: Principal = Depends(get_principal),
    auth: AuthService = Depends(get_auth_service),
) -> None:
    await auth.logout(principal, body.refresh_token, audit_context(request, principal), all_sessions=body.all_sessions)
    await auth.session.commit()


@router.get("/me", response_model=MeOut)
async def me(principal: Principal = Depends(get_principal)) -> MeOut:
    user = principal.user
    return MeOut(
        id=user.id,
        email=user.email,
        status=user.status,
        role=user.role.name,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        permissions=sorted(p.value for p in principal.permissions),
        authenticated_via=principal.via,
    )
