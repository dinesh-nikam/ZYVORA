from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.deps import (
    audit_context,
    get_container,
    get_investigation_service,
    idempotency_key,
    rate_limit,
    require,
)
from app.core.errors import ValidationFailed
from app.core.permissions import Permission
from app.models import Investigation
from app.models.enums import AuthorizationBasis, InvestigationStatus, VerificationStatus
from app.repositories.investigations import ProfileRepository
from app.schemas.common import ERROR_RESPONSES, PageOut
from app.schemas.investigation import (
    DiscoveryIn,
    InvestigationCreateIn,
    InvestigationListItem,
    InvestigationOut,
    InvestigationStatusOut,
    JobOut,
)
from app.schemas.profile import ProfileOut
from app.schemas.report import InvestigationReport
from app.services import audit
from app.services.auth import Principal
from app.services.container import Container
from app.services.investigations import InvestigationService, owner_scope, validate_discovery_request
from app.services.reporting.builder import ReportBuilder
from app.services.reporting.exporters import MEDIA_TYPES, ExportFormat, export

router = APIRouter(prefix="/investigations", tags=["investigations"], responses=ERROR_RESPONSES)


def _out(inv: Investigation) -> InvestigationOut:
    return InvestigationOut.model_validate(inv)


@router.post("", response_model=InvestigationOut, status_code=status.HTTP_201_CREATED)
async def create_investigation(
    body: InvestigationCreateIn,
    request: Request,
    principal: Principal = Depends(require(Permission.INVESTIGATION_CREATE)),
    service: InvestigationService = Depends(get_investigation_service),
) -> InvestigationOut:
    """Open an investigation.  A purpose, an authorization basis and an explicit acknowledgement of the
    platform's limitations are mandatory; the number must be valid."""
    if not body.acknowledge_limitations:
        raise ValidationFailed("You must acknowledge the investigation limitations", code="acknowledgement_required")
    inv = await service.create(
        principal, phone=body.phone, region=body.region, purpose=body.purpose,
        authorization_basis=body.authorization_basis, authorization_reference=body.authorization_reference,
        ctx=audit_context(request, principal),
    )
    return _out(inv)


@router.get("", response_model=PageOut[InvestigationListItem])
async def list_investigations(
    principal: Principal = Depends(require(Permission.INVESTIGATION_READ)),
    service: InvestigationService = Depends(get_investigation_service),
    container: Container = Depends(get_container),
    status_filter: InvestigationStatus | None = Query(default=None, alias="status"),
    authorization_basis: AuthorizationBasis | None = None,
    phone: str | None = Query(default=None, max_length=32, description="Filter by number (exact match)"),
    reference: str | None = Query(default=None, max_length=32, pattern=r"^[A-Za-z0-9\-]+$"),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    sort: str = Query(default="-created_at", max_length=32),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=20, ge=1, le=100),
) -> PageOut[InvestigationListItem]:
    phone_hash = service.phone_hash(container.phone.analyze(phone).e164) if phone else None
    result = await service.investigations.list(
        owner_id=principal.id, status=status_filter.value if status_filter else None,
        authorization_basis=authorization_basis.value if authorization_basis else None, phone_hash=phone_hash,
        reference=reference, created_from=created_from, created_to=created_to, sort=sort, page=page,
        page_size=page_size,
    )
    items = [
        InvestigationListItem(
            id=i.id, investigation_reference=i.investigation_reference, normalized_phone=i.normalized_phone,
            authorization_basis=i.authorization_basis, status=i.status, created_at=i.created_at,
            completed_at=i.completed_at,
            profile_count=int((i.summary or {}).get("counts", {}).get("total_profiles", 0)),
        )
        for i in result.items
    ]
    return PageOut(items=items, total=result.total, page=result.page, page_size=result.page_size)


@router.get("/{investigation_id}", response_model=InvestigationStatusOut)
async def get_investigation(
    investigation_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Permission.INVESTIGATION_READ)),
    service: InvestigationService = Depends(get_investigation_service),
) -> InvestigationStatusOut:
    inv = await service.get(principal, investigation_id, audit_context(request, principal))
    job = await service.latest_job(inv.id)
    return InvestigationStatusOut(investigation=_out(inv), latest_job=JobOut.model_validate(job) if job else None)


@router.delete("/{investigation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_investigation(
    investigation_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Permission.INVESTIGATION_DELETE)),
    service: InvestigationService = Depends(get_investigation_service),
) -> None:
    """Erase the number, purpose, phone intelligence, profiles and evidence immediately; a content-free
    tombstone is kept for audit linkage and purged after the tombstone retention period."""
    await service.delete(principal, investigation_id, audit_context(request, principal))


@router.post("/{investigation_id}/cancel", response_model=InvestigationOut, status_code=status.HTTP_202_ACCEPTED)
async def cancel_investigation(
    investigation_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Permission.DISCOVERY_RUN)),
    service: InvestigationService = Depends(get_investigation_service),
) -> InvestigationOut:
    return _out(await service.cancel(principal, investigation_id, audit_context(request, principal)))


@router.post("/{investigation_id}/profile-discovery", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(rate_limit("discovery", "rate_limit_discovery"))])
async def start_profile_discovery(
    investigation_id: uuid.UUID,
    body: DiscoveryIn,
    request: Request,
    response: Response,
    principal: Principal = Depends(require(Permission.DISCOVERY_RUN)),
    service: InvestigationService = Depends(get_investigation_service),
    key: str | None = Depends(idempotency_key),
) -> JobOut:
    """Queue public-source discovery.  Send an ``Idempotency-Key`` header to make retries safe; only one
    active discovery job per investigation is allowed."""
    req = validate_discovery_request(body.platforms, body.profile_urls, body.include_web_search, body.live_checks)
    job, created = await service.start_discovery(principal, investigation_id, req, key,
                                                 audit_context(request, principal))
    if not created:
        response.headers["Idempotent-Replayed"] = "true"
        await service.session.refresh(job)
    return JobOut.model_validate(job)


@router.get("/{investigation_id}/profiles", response_model=PageOut[ProfileOut])
async def list_profiles(
    investigation_id: uuid.UUID,
    principal: Principal = Depends(require(Permission.INVESTIGATION_READ)),
    service: InvestigationService = Depends(get_investigation_service),
    platform: str | None = Query(default=None, max_length=32, pattern=r"^[a-z_]+$"),
    verification_status: VerificationStatus | None = Query(default=None, alias="status"),
    min_confidence: int | None = Query(default=None, ge=0, le=100),
    sort: str = Query(default="-confidence", max_length=32),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=50, ge=1, le=200),
) -> PageOut[ProfileOut]:
    inv = await service.get(principal, investigation_id)
    result = await ProfileRepository(service.session).list(
        inv.id, platform=platform, status=verification_status.value if verification_status else None,
        min_confidence=min_confidence, sort=sort, page=page, page_size=page_size,
    )
    return PageOut(items=[ProfileOut.model_validate(p) for p in result.items], total=result.total,
                   page=result.page, page_size=result.page_size)


async def _report(service: InvestigationService, principal: Principal, investigation_id: uuid.UUID,
                  request: Request, mask_phone: bool, action: str, fmt: str) -> InvestigationReport:
    principal.require(Permission.REPORT_READ)
    inv = await service.investigations.get(investigation_id, owner_id=owner_scope(principal))
    if inv is None:
        from app.core.errors import NotFound  # noqa: PLC0415

        raise NotFound("Investigation not found")
    report = await ReportBuilder(service.session).build(inv, mask_phone=mask_phone)
    audit.record(service.session, audit_context(request, principal), action, "investigation", inv.id,
                 format=fmt, masked=mask_phone, owner_access=inv.user_id == principal.id)
    await service.session.commit()
    return report


@router.get("/{investigation_id}/report", response_model=InvestigationReport)
async def get_report(
    investigation_id: uuid.UUID,
    request: Request,
    mask_phone: bool = False,
    principal: Principal = Depends(require(Permission.REPORT_READ)),
    service: InvestigationService = Depends(get_investigation_service),
) -> InvestigationReport:
    return await _report(service, principal, investigation_id, request, mask_phone, "report.view", "json")


@router.get("/{investigation_id}/report/export", response_class=Response,
            responses={200: {"content": {v: {} for v in MEDIA_TYPES.values()},
                             "description": "Report file download"}})
async def export_report(
    investigation_id: uuid.UUID,
    request: Request,
    export_format: ExportFormat = Query(default=ExportFormat.PDF, alias="format"),
    mask_phone: bool = False,
    principal: Principal = Depends(require(Permission.REPORT_READ)),
    service: InvestigationService = Depends(get_investigation_service),
    container: Container = Depends(get_container),
) -> Response:
    report = await _report(service, principal, investigation_id, request, mask_phone, "report.export",
                           export_format.value)
    payload = export(report, export_format, pdf_font_path=container.settings.pdf_font_path)
    filename = f"{report.investigation_reference}.{export_format.value}"
    return Response(
        content=payload,
        media_type=MEDIA_TYPES[export_format],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )
