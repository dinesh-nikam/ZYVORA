from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.api.deps import audit_context, get_investigation_service, rate_limit, require
from app.core.permissions import Permission
from app.schemas.common import ERROR_RESPONSES
from app.schemas.profile import ProfileOut, VerifyUrlIn, VerifyUrlOut
from app.services.auth import Principal
from app.services.investigations import InvestigationService

router = APIRouter(prefix="/profiles", tags=["profiles"], responses=ERROR_RESPONSES)


@router.post("/verify-url", response_model=VerifyUrlOut,
             dependencies=[Depends(rate_limit("verify", "rate_limit_verify"))])
async def verify_url(body: VerifyUrlIn, request: Request,
                     principal: Principal = Depends(require(Permission.PROFILE_VERIFY)),
                     service: InvestigationService = Depends(get_investigation_service)) -> VerifyUrlOut:
    """Run the 10-stage verification pipeline on a single URL (not stored).  Only allowlisted social
    platforms are contacted, through the SSRF-guarded client."""
    o = await service.verify_url(principal, body.url, body.phone, audit_context(request, principal))
    return VerifyUrlOut(
        input_url=o.input_url, platform=o.platform, platform_name=o.platform_name, kind=o.kind, handle=o.handle,
        canonical_url=o.canonical_url, display_name=o.display_name, verification_status=o.status.value,
        confidence_score=o.confidence.score, confidence_level=o.confidence.level.value,
        confidence_factors=o.confidence.factors, evidence_description=o.evidence_description(),
        http_status=o.http_status, redirect_chain=o.redirect_chain, stages=[s.as_dict() for s in o.stages],
        evidence=[e.as_dict() for e in o.evidence], limitations=list(dict.fromkeys(o.limitations)),
        checked_at=o.checked_at,
    )


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile(profile_id: uuid.UUID,
                      principal: Principal = Depends(require(Permission.INVESTIGATION_READ)),
                      service: InvestigationService = Depends(get_investigation_service)) -> ProfileOut:
    return ProfileOut.model_validate(await service.get_profile(principal, profile_id))


@router.post("/{profile_id}/verify", response_model=ProfileOut,
             dependencies=[Depends(rate_limit("verify", "rate_limit_verify"))])
async def reverify_profile(profile_id: uuid.UUID, request: Request,
                           principal: Principal = Depends(require(Permission.PROFILE_VERIFY)),
                           service: InvestigationService = Depends(get_investigation_service)) -> ProfileOut:
    """Re-run verification (including permitted live checks) for a stored profile."""
    profile = await service.reverify_profile(principal, profile_id, audit_context(request, principal))
    return ProfileOut.model_validate(profile)
