from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import audit_context, get_investigation_service, require
from app.core.permissions import Permission
from app.schemas.common import ERROR_RESPONSES
from app.services.auth import Principal
from app.services.investigations import InvestigationService

router = APIRouter(prefix="/me", tags=["privacy (data subject requests)"], responses=ERROR_RESPONSES)


@router.get("/export")
async def export_my_data(
    request: Request,
    principal: Principal = Depends(require(Permission.SELF_DATA_MANAGE)),
    service: InvestigationService = Depends(get_investigation_service),
) -> dict[str, Any]:
    """Machine-readable export of the caller's account and investigation data."""
    return await service.export_user_data(principal, audit_context(request, principal))


@router.delete("/data")
async def delete_my_data(
    request: Request,
    include_account: bool = Query(default=False, description="Also close and anonymise the account"),
    principal: Principal = Depends(require(Permission.SELF_DATA_MANAGE)),
    service: InvestigationService = Depends(get_investigation_service),
) -> dict[str, Any]:
    """Erase all of the caller's investigations (and optionally the account)."""
    count = await service.erase_user_data(principal, audit_context(request, principal), include_account=include_account)
    return {"investigations_erased": count, "account_closed": include_account}
