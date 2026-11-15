from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.schemas.common import APIModel


class EvidenceOut(APIModel):
    id: uuid.UUID | None = None
    source_url: str
    source_type: str
    source_name: str
    source_reliability: str
    evidence_summary: str
    phone_reference_found: bool
    collected_at: datetime
    expiration_at: datetime | None = None


class ProfileOut(APIModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    platform: str
    profile_url: str
    canonical_url: str
    handle: str | None
    discovery_source: str
    evidence_description: str
    verification_status: str
    confidence_score: int
    confidence_level: str
    confidence_factors: list[dict[str, Any]]
    verification_stages: list[dict[str, Any]]
    limitations: list[str]
    discovered_at: datetime
    verified_at: datetime | None
    evidence: list[EvidenceOut]


class VerifyUrlIn(APIModel):
    url: str = Field(min_length=8, max_length=2048, examples=["https://t.me/telegram"])
    phone: str | None = Field(default=None, max_length=32,
                              description="Optional number to look for in the profile's public content")


class VerifyUrlOut(APIModel):
    input_url: str
    platform: str | None
    platform_name: str | None
    kind: str | None
    handle: str | None
    canonical_url: str | None
    display_name: str | None
    verification_status: str
    confidence_score: int
    confidence_level: str
    confidence_factors: list[dict[str, Any]]
    evidence_description: str
    http_status: int | None
    redirect_chain: list[dict[str, Any]]
    stages: list[dict[str, str]]
    evidence: list[dict[str, Any]]
    limitations: list[str]
    checked_at: datetime
