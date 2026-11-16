from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.models.enums import AuthorizationBasis
from app.schemas.common import APIModel


class InvestigationCreateIn(APIModel):
    phone: str = Field(min_length=3, max_length=32, examples=["+919876543210"])
    region: str | None = Field(default=None, min_length=2, max_length=2, pattern=r"^[A-Za-z]{2}$")
    purpose: str = Field(
        min_length=10, max_length=1000, description="Why this investigation is necessary (stored encrypted)"
    )
    authorization_basis: AuthorizationBasis
    authorization_reference: str | None = Field(
        default=None, max_length=200, description="Case/ticket/consent reference (stored encrypted)"
    )
    acknowledge_limitations: bool = Field(
        description="Must be true: the investigator acknowledges results are public-source indications, not "
        "proof of identity, and that the stated authorization basis applies."
    )


class PhoneIntelligenceOut(APIModel):
    country_code: int | None
    region_code: str | None
    country_name: str | None
    number_type: str
    validation_status: str
    carrier: str | None
    metadata_provider: str
    details: dict[str, Any]
    checked_at: datetime


class InvestigationOut(APIModel):
    id: uuid.UUID
    investigation_reference: str
    normalized_phone: str | None
    purpose: str | None
    authorization_basis: str
    authorization_reference: str | None
    status: str
    summary: dict[str, Any]
    phone_intelligence: PhoneIntelligenceOut | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime


class InvestigationListItem(APIModel):
    id: uuid.UUID
    investigation_reference: str
    normalized_phone: str | None
    authorization_basis: str
    status: str
    created_at: datetime
    completed_at: datetime | None
    profile_count: int = 0


class DiscoveryIn(APIModel):
    platforms: list[str] | None = Field(
        default=None, max_length=32, description="Platform keys; default = all supported platforms"
    )
    include_web_search: bool = True
    profile_urls: list[str] | None = Field(
        default=None, max_length=25, description="Investigator-supplied profile URLs to verify"
    )
    live_checks: bool = Field(
        default=True, description="Perform permitted live checks (official APIs, robots-permitted public pages)"
    )


class JobOut(APIModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    job_type: str
    status: str
    retry_count: int
    progress: dict[str, Any]
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    cancel_requested_at: datetime | None


class InvestigationStatusOut(APIModel):
    investigation: InvestigationOut
    latest_job: JobOut | None
