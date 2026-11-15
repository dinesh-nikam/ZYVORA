from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ReportPhone(BaseModel):
    normalized_number: str | None
    country_code: int | None
    country: str | None
    region_code: str | None
    number_type: str | None
    validation_status: str | None
    carrier: str | None
    metadata_provider: str | None
    checked_at: datetime | None


class ReportEvidence(BaseModel):
    source_url: str
    discovery_method: str
    source_name: str
    source_reliability: str
    summary: str
    phone_reference_found: bool
    timestamp: datetime


class ReportProfile(BaseModel):
    platform: str
    profile_url: str
    canonical_url: str
    discovery_source: str
    evidence_description: str
    verification_status: str
    confidence_level: str
    confidence_score: int
    confidence_factors: list[dict[str, Any]]
    attribution_limitations: list[str]
    verified_at: datetime | None
    evidence: list[ReportEvidence]


class ReportSource(BaseModel):
    source_url: str
    discovery_method: str
    source_name: str
    timestamp: datetime
    source_reliability: str


class ReportSummary(BaseModel):
    discovered_urls: int
    verified_public_references: int
    public_candidates: int
    unverified: int
    inaccessible: int
    not_found: int
    restricted: int
    errors: int
    search: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str]


class InvestigationReport(BaseModel):
    report_version: str = "1.0"
    generated_at: datetime
    investigation_reference: str
    investigation_status: str
    authorization_basis: str
    purpose: str | None
    created_at: datetime
    completed_at: datetime | None
    phone_intelligence: ReportPhone
    profiles: list[ReportProfile]
    sources: list[ReportSource]
    summary: ReportSummary
    methodology: list[str]
    disclaimer: str
