from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import audit_context, get_auth_service, require
from app.core.permissions import Permission
from app.schemas.auth import ApiKeyCreatedOut, ApiKeyCreateIn, ApiKeyOut
from app.schemas.common import ERROR_RESPONSES
from app.services.auth import AuthService, Principal

router = APIRouter(prefix="/api-keys", tags=["api keys"], responses=ERROR_RESPONSES)


@router.post("", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreateIn,
    request: Request,
    principal: Principal = Depends(require(Permission.APIKEY_MANAGE)),
    auth: AuthService = Depends(get_auth_service),
) -> ApiKeyCreatedOut:
    """Create an API key.  The plaintext key is returned exactly once; only its SHA-256 hash is stored."""
    key, plaintext = await auth.create_api_key(
        principal, body.name, body.permissions, body.expires_in_days, audit_context(request, principal)
    )
    await auth.session.commit()
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(key).model_dump(), api_key=plaintext)


@router.get("", response_model=list[ApiKeyOut])
async def list_api_keys(
    principal: Principal = Depends(require(Permission.APIKEY_MANAGE)), auth: AuthService = Depends(get_auth_service)
) -> list[ApiKeyOut]:
    return [ApiKeyOut.model_validate(k) for k in await auth.api_keys.for_user(principal.id)]


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Permission.APIKEY_MANAGE)),
    auth: AuthService = Depends(get_auth_service),
) -> None:
    await auth.revoke_api_key(principal, key_id, audit_context(request, principal))
    await auth.session.commit()
