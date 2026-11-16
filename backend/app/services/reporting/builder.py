"""Builds the structured investigation report from persisted data."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.models import Investigation
from app.models.enums import EvidenceSourceType, VerificationStatus
from app.repositories.investigations import ProfileRepository
from app.schemas.report import (
    InvestigationReport,
    ReportEvidence,
    ReportPhone,
    ReportProfile,
    ReportSource,
    ReportSummary,
)
from app.services.phone.service import LIMITATIONS as PHONE_LIMITATIONS

METHODOLOGY = [
    "Phone metadata comes from libphonenumber numbering-plan data (and a licensed carrier API when configured).",
    "Profile links come only from: authorised public search APIs, investigator-supplied URLs, official platform "
    "APIs and robots-permitted public pages. No authentication, privacy setting, CAPTCHA or rate limit is bypassed.",
    "No phone-number-to-account lookup (contact discovery) is performed on any platform.",
    "A profile is VERIFIED_PUBLIC_REFERENCE only when its own public content, checked live or through an official "
    "API, explicitly contains the investigated number.",
    "PUBLIC_CANDIDATE means public sources link the URL to the number but the profile was not live-confirmed.",
    "Confidence is the sum of documented factors; name or username similarity is never used.",
    "Evidence excerpts are minimised: e-mail addresses and unrelated phone numbers are redacted.",
]

DISCLAIMER = (
    "This report lists publicly documented references only. A public reference does not prove that the holder of "
    "the number owns or controls a profile, and absence of results does not prove absence of accounts. Use only "
    "under the declared authorization basis and applicable law."
)

_METHOD = {
    EvidenceSourceType.SEARCH_RESULT.value: "public_search_index",
    EvidenceSourceType.LIVE_PROFILE_PAGE.value: "live_public_page",
    EvidenceSourceType.OFFICIAL_PLATFORM_API.value: "official_platform_api",
    EvidenceSourceType.USER_SUBMISSION.value: "investigator_supplied",
    EvidenceSourceType.GENERATED_LINK.value: "generated_official_link",
}


class ReportBuilder:
    def __init__(self, session: AsyncSession) -> None:
        self.profiles = ProfileRepository(session)

    async def build(self, investigation: Investigation, *, mask_phone: bool = False) -> InvestigationReport:
        intel = investigation.phone_intelligence
        number = investigation.normalized_phone
        if mask_phone and number:
            number = number[:4] + "*" * max(len(number) - 6, 0) + number[-2:]
        phone = ReportPhone(
            normalized_number=number,
            country_code=intel.country_code if intel else investigation.phone_country_code,
            country=intel.country_name if intel else None,
            region_code=intel.region_code if intel else None,
            number_type=intel.number_type if intel else None,
            validation_status=intel.validation_status if intel else None,
            carrier=intel.carrier if intel else None,
            metadata_provider=intel.metadata_provider if intel else None,
            checked_at=intel.checked_at if intel else None,
        )
        rows = await self.profiles.all_for(investigation.id)
        profiles: list[ReportProfile] = []
        sources: dict[tuple[str, str], ReportSource] = {}
        counts = {s.value: 0 for s in VerificationStatus}
        for row in rows:
            counts[row.verification_status] = counts.get(row.verification_status, 0) + 1
            evidence = []
            for ev in row.evidence:
                method = _METHOD.get(ev.source_type, ev.source_type)
                evidence.append(
                    ReportEvidence(
                        source_url=ev.source_url,
                        discovery_method=method,
                        source_name=ev.source_name,
                        source_reliability=ev.source_reliability,
                        summary=ev.evidence_summary,
                        phone_reference_found=ev.phone_reference_found,
                        timestamp=ev.collected_at,
                    )
                )
                sources.setdefault(
                    (ev.source_url, method),
                    ReportSource(
                        source_url=ev.source_url,
                        discovery_method=method,
                        source_name=ev.source_name,
                        timestamp=ev.collected_at,
                        source_reliability=ev.source_reliability,
                    ),
                )
            profiles.append(
                ReportProfile(
                    platform=row.platform,
                    profile_url=row.profile_url,
                    canonical_url=row.canonical_url,
                    discovery_source=row.discovery_source,
                    evidence_description=row.evidence_description,
                    verification_status=row.verification_status,
                    confidence_level=row.confidence_level,
                    confidence_score=row.confidence_score,
                    confidence_factors=row.confidence_factors,
                    attribution_limitations=row.limitations,
                    verified_at=row.verified_at,
                    evidence=evidence,
                )
            )
        stored = investigation.summary or {}
        limitations = list(dict.fromkeys([*PHONE_LIMITATIONS, *stored.get("limitations", [])]))
        if investigation.status != "completed":
            limitations.append(f"Investigation status is '{investigation.status}'; results may be incomplete.")
        summary = ReportSummary(
            discovered_urls=len(rows),
            verified_public_references=counts[VerificationStatus.VERIFIED_PUBLIC_REFERENCE.value],
            public_candidates=counts[VerificationStatus.PUBLIC_CANDIDATE.value],
            unverified=counts[VerificationStatus.UNVERIFIED.value],
            inaccessible=counts[VerificationStatus.INACCESSIBLE.value],
            not_found=counts[VerificationStatus.NOT_FOUND.value],
            restricted=counts[VerificationStatus.RESTRICTED.value],
            errors=counts[VerificationStatus.ERROR.value],
            search=stored.get("search", {}),
            limitations=limitations,
        )
        return InvestigationReport(
            generated_at=utcnow(),
            investigation_reference=investigation.investigation_reference,
            investigation_status=investigation.status,
            authorization_basis=investigation.authorization_basis,
            purpose=investigation.purpose,
            created_at=investigation.created_at,
            completed_at=investigation.completed_at,
            phone_intelligence=phone,
            profiles=profiles,
            sources=sorted(sources.values(), key=lambda s: s.timestamp),
            summary=summary,
            methodology=METHODOLOGY,
            disclaimer=DISCLAIMER,
        )
