from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import audit_context, get_container, get_session, rate_limit, require
from app.core.permissions import Permission
from app.schemas.common import ERROR_RESPONSES
from app.schemas.phone import PhoneIn, PhoneMetadataOut, PhoneValidationOut
from app.services import audit
from app.services.auth import Principal
from app.services.container import Container

router = APIRouter(prefix="/phone", tags=["phone intelligence"], responses=ERROR_RESPONSES,
                   dependencies=[Depends(rate_limit("phone", "rate_limit_phone"))])


@router.post("/validate", response_model=PhoneValidationOut)
async def validate(body: PhoneIn, request: Request,
                   principal: Principal = Depends(require(Permission.PHONE_VALIDATE)),
                   container: Container = Depends(get_container),
                   session: AsyncSession = Depends(get_session)) -> PhoneValidationOut:
    """Parse, normalise (E.164) and classify a number.  Stateless: the number is not stored."""
    intel = container.phone.analyze(body.phone, body.region)
    audit.record(session, audit_context(request, principal), "phone.validate", "phone", None,
                 country_code=intel.country_code, result=intel.validation_status.value)
    await session.commit()
    return PhoneValidationOut(
        e164=intel.e164, international=intel.international, national=intel.national,
        country_code=intel.country_code, region_code=intel.region_code, country_name=intel.country_name,
        number_type=intel.number_type, validation_status=intel.validation_status.value, is_valid=intel.is_valid,
        is_possible=intel.is_possible, possibility_reason=intel.possibility_reason, limitations=intel.limitations,
    )


@router.post("/metadata", response_model=PhoneMetadataOut)
async def metadata(body: PhoneIn, request: Request,
                   principal: Principal = Depends(require(Permission.PHONE_VALIDATE)),
                   container: Container = Depends(get_container),
                   session: AsyncSession = Depends(get_session)) -> PhoneMetadataOut:
    """Full numbering-plan metadata, including original-allocation carrier and time zones."""
    intel = container.phone.analyze(body.phone, body.region)
    carrier, carrier_source = intel.carrier, "libphonenumber original range allocation"
    if container.carrier_provider is not None and intel.is_valid:
        lookup = await container.carrier_provider.lookup(intel.e164)
        if lookup.carrier:
            carrier, carrier_source = lookup.carrier, lookup.provider
    audit.record(session, audit_context(request, principal), "phone.metadata", "phone", None,
                 country_code=intel.country_code, result=intel.validation_status.value)
    await session.commit()
    return PhoneMetadataOut(
        e164=intel.e164, international=intel.international, national=intel.national, rfc3966=intel.rfc3966,
        country_code=intel.country_code, region_code=intel.region_code, country_name=intel.country_name,
        region_description=intel.region_description, number_type=intel.number_type,
        validation_status=intel.validation_status.value, is_valid=intel.is_valid, is_possible=intel.is_possible,
        possibility_reason=intel.possibility_reason, timezones=intel.timezones, carrier=carrier,
        carrier_source=carrier_source, metadata_provider=intel.metadata_provider, checked_at=intel.checked_at,
        limitations=intel.limitations,
    )
