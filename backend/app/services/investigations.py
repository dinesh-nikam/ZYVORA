"""Investigation orchestrator: lifecycle, ownership enforcement, discovery
job creation, profile re-verification, data export and erasure."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound, ValidationFailed
from app.core.metrics import INVESTIGATIONS_CREATED
from app.core.permissions import Permission
from app.core.security import utcnow
from app.models import (
    DiscoveredProfile,
    EvidenceSource,
    Investigation,
    InvestigationJob,
    PhoneIntelligence,
    User,
)
from app.models.enums import (
    AuthorizationBasis,
    DiscoverySource,
    InvestigationStatus,
    JobStatus,
    JobType,
    UserStatus,
)
from app.repositories.investigations import InvestigationRepository, JobRepository, ProfileRepository
from app.repositories.users import ApiKeyRepository, TelegramLinkRepository, TokenRepository
from app.services import audit
from app.services.audit import AuditContext
from app.services.auth import Principal
from app.services.discovery.platforms import DEFAULT_PLATFORMS, PLATFORMS
from app.services.events import EventType
from app.services.phone.matching import build_variants
from app.services.verification.engine import VerificationOutcome

if TYPE_CHECKING:
    from app.services.container import Container

MAX_USER_PROFILE_URLS = 25


def new_reference() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return f"PI-{datetime.now(UTC):%Y%m%d}-" + "".join(secrets.choice(alphabet) for _ in range(6))


def owner_scope(principal: Principal) -> uuid.UUID | None:
    """Admins with ``investigation:read_any`` may read across users (audited); everyone else is scoped."""
    return None if principal.has(Permission.INVESTIGATION_READ_ANY) else principal.id


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    platforms: list[str]
    include_web_search: bool
    profile_urls: list[str]
    live_checks: bool = True


def validate_discovery_request(platforms: list[str] | None, profile_urls: list[str] | None,
                               include_web_search: bool, live_checks: bool) -> DiscoveryRequest:
    selected = list(dict.fromkeys(p.lower() for p in (platforms or DEFAULT_PLATFORMS)))
    unknown = [p for p in selected if p not in PLATFORMS]
    if unknown:
        raise ValidationFailed("Unknown platform", code="invalid_platform",
                               details=[{"unknown": unknown, "supported": sorted(PLATFORMS)}])
    urls = [u.strip() for u in (profile_urls or []) if u and u.strip()]
    if len(urls) > MAX_USER_PROFILE_URLS:
        raise ValidationFailed(f"At most {MAX_USER_PROFILE_URLS} profile URLs per request", code="too_many_urls")
    if any(len(u) > 2048 for u in urls):
        raise ValidationFailed("Profile URL too long", code="invalid_url")
    return DiscoveryRequest(platforms=selected, include_web_search=include_web_search,
                            profile_urls=list(dict.fromkeys(urls)), live_checks=live_checks)


class InvestigationService:
    def __init__(self, session: AsyncSession, container: Container) -> None:
        self.session = session
        self.c = container
        self.investigations = InvestigationRepository(session)
        self.profiles = ProfileRepository(session)
        self.jobs = JobRepository(session)

    def phone_hash(self, e164: str) -> str:
        return self.c.indexer.digest(e164, purpose="phone")

    # ----------------------------------------------------------------- create
    async def create(
        self,
        principal: Principal,
        *,
        phone: str,
        region: str | None,
        purpose: str,
        authorization_basis: AuthorizationBasis,
        authorization_reference: str | None,
        ctx: AuditContext,
    ) -> Investigation:
        principal.require(Permission.INVESTIGATION_CREATE)
        intel = self.c.phone.require_investigable(phone, region)
        carrier = intel.carrier
        provider = intel.metadata_provider
        details = intel.details()
        if self.c.carrier_provider is not None:
            lookup = await self.c.carrier_provider.lookup(intel.e164)
            details["licensed_carrier_lookup"] = {"provider": lookup.provider, "line_type": lookup.line_type,
                                                  "error": lookup.error}
            if lookup.carrier:
                carrier, provider = lookup.carrier, f"{lookup.provider}; {intel.metadata_provider}"
        now = utcnow()
        investigation = await self.investigations.add(Investigation(
            user_id=principal.id,
            investigation_reference=new_reference(),
            normalized_phone=intel.e164,
            phone_lookup_hash=self.phone_hash(intel.e164),
            phone_country_code=intel.country_code,
            purpose=purpose.strip(),
            authorization_basis=authorization_basis.value,
            authorization_reference=(authorization_reference or "").strip() or None,
            status=InvestigationStatus.CREATED.value,
            summary={"limitations": intel.limitations},
            expires_at=now + timedelta(days=self.c.settings.retention_investigation_days),
        ))
        await self.investigations.set_phone_intelligence(PhoneIntelligence(
            investigation_id=investigation.id,
            country_code=intel.country_code,
            region_code=intel.region_code,
            country_name=intel.country_name,
            number_type=intel.number_type,
            validation_status=intel.validation_status.value,
            carrier=carrier,
            metadata_provider=provider,
            details=details,
            checked_at=intel.checked_at,
        ))
        audit.record(self.session, ctx, "investigation.create", "investigation", investigation.id,
                     reference=investigation.investigation_reference, basis=authorization_basis.value,
                     country_code=intel.country_code)
        await self.session.commit()
        await self.session.refresh(investigation, ["phone_intelligence"])
        INVESTIGATIONS_CREATED.inc()
        await self.c.events.publish(investigation.id, EventType.INVESTIGATION_CREATED,
                                    {"reference": investigation.investigation_reference})
        return investigation

    # ------------------------------------------------------------------- read
    async def get(self, principal: Principal, investigation_id: uuid.UUID, ctx: AuditContext | None = None,
                  ) -> Investigation:
        principal.require(Permission.INVESTIGATION_READ)
        investigation = await self.investigations.get(investigation_id, owner_id=owner_scope(principal))
        if investigation is None:
            raise NotFound("Investigation not found")
        if ctx is not None and investigation.user_id != principal.id:
            audit.record(self.session, ctx, "investigation.read_other_user", "investigation", investigation.id,
                         owner_id=str(investigation.user_id))
            await self.session.commit()
        return investigation

    # ----------------------------------------------------------------- delete
    async def delete(self, principal: Principal, investigation_id: uuid.UUID, ctx: AuditContext) -> None:
        principal.require(Permission.INVESTIGATION_DELETE)
        investigation = await self.investigations.get(investigation_id, owner_id=principal.id, for_update=True)
        if investigation is None:
            raise NotFound("Investigation not found")
        await self._erase(investigation)
        audit.record(self.session, ctx, "investigation.delete", "investigation", investigation_id)
        await self.session.commit()
        await self.c.events.clear(investigation_id)

    async def _erase(self, investigation: Investigation) -> None:
        now = utcnow()
        job_ids = await self.jobs.mark_cancel_requested_for(investigation.id, now)
        await self.investigations.erase_content(investigation, now)
        if investigation.status in (InvestigationStatus.QUEUED.value, InvestigationStatus.RUNNING.value):
            investigation.status = InvestigationStatus.CANCELLED.value
        for job_id in job_ids:
            await self.c.jobs.abort(job_id)

    # ----------------------------------------------------------------- cancel
    async def cancel(self, principal: Principal, investigation_id: uuid.UUID, ctx: AuditContext) -> Investigation:
        principal.require(Permission.DISCOVERY_RUN)
        investigation = await self.investigations.get(investigation_id, owner_id=principal.id, for_update=True)
        if investigation is None:
            raise NotFound("Investigation not found")
        job_ids = await self.jobs.mark_cancel_requested_for(investigation.id, utcnow())
        if not job_ids:
            raise Conflict("No running discovery job to cancel", code="nothing_to_cancel")
        audit.record(self.session, ctx, "discovery.cancel", "investigation", investigation.id)
        await self.session.commit()
        for job_id in job_ids:
            await self.c.jobs.abort(job_id)
        return investigation

    # -------------------------------------------------------------- discovery
    async def start_discovery(
        self,
        principal: Principal,
        investigation_id: uuid.UUID,
        request: DiscoveryRequest,
        idempotency_key: str | None,
        ctx: AuditContext,
    ) -> tuple[InvestigationJob, bool]:
        """Create (or return the existing) discovery job.  Returns ``(job, created)``."""
        principal.require(Permission.DISCOVERY_RUN)
        investigation = await self.investigations.get(investigation_id, owner_id=principal.id, for_update=True)
        if investigation is None:
            raise NotFound("Investigation not found")
        if idempotency_key:
            previous = await self.jobs.by_idempotency_key(investigation.id, idempotency_key)
            if previous is not None:
                return previous, False
        active = await self.jobs.active_for(investigation.id, JobType.PROFILE_DISCOVERY.value)
        if active is not None:
            raise Conflict("A discovery job is already running for this investigation", code="job_already_active",
                           details=[{"job_id": str(active.id)}])
        job = InvestigationJob(
            investigation_id=investigation.id,
            job_type=JobType.PROFILE_DISCOVERY.value,
            status=JobStatus.QUEUED.value,
            idempotency_key=idempotency_key,
            parameters={"platforms": request.platforms, "include_web_search": request.include_web_search,
                        "profile_urls": request.profile_urls, "live_checks": request.live_checks},
            progress={},
        )
        try:
            await self.jobs.add(job)
        except IntegrityError as exc:  # concurrent request won the partial-unique index race
            await self.session.rollback()
            raise Conflict("A discovery job is already running for this investigation",
                           code="job_already_active") from exc
        investigation.status = InvestigationStatus.QUEUED.value
        audit.record(self.session, ctx, "discovery.start", "investigation", investigation.id, job_id=str(job.id),
                     platforms=request.platforms, web_search=request.include_web_search,
                     user_urls=len(request.profile_urls))
        await self.session.commit()
        await self.c.jobs.enqueue_discovery(job.id)
        return job, True

    async def latest_job(self, investigation_id: uuid.UUID) -> InvestigationJob | None:
        return await self.jobs.latest_for(investigation_id)

    # ----------------------------------------------------- profile operations
    async def get_profile(self, principal: Principal, profile_id: uuid.UUID) -> DiscoveredProfile:
        principal.require(Permission.INVESTIGATION_READ)
        profile = await self.profiles.get_owned(profile_id, owner_scope(principal))
        if profile is None:
            raise NotFound("Profile not found")
        return profile

    async def reverify_profile(self, principal: Principal, profile_id: uuid.UUID, ctx: AuditContext,
                               ) -> DiscoveredProfile:
        principal.require(Permission.PROFILE_VERIFY)
        profile = await self.profiles.get_owned(profile_id, principal.id)
        if profile is None:
            raise NotFound("Profile not found")
        investigation = await self.investigations.get(profile.investigation_id, owner_id=principal.id)
        if investigation is None or investigation.normalized_phone is None:
            raise NotFound("Profile not found")
        if profile.discovery_source == DiscoverySource.GENERATED_OFFICIAL_LINK.value:
            raise Conflict("Generated links are not re-verifiable", code="not_verifiable")
        from app.models.enums import EvidenceSourceType, SourceReliability  # noqa: PLC0415
        from app.services.verification.engine import EvidenceItem  # noqa: PLC0415

        prior = [
            EvidenceItem(source_url=e.source_url, source_type=EvidenceSourceType(e.source_type),
                         source_name=e.source_name, reliability=SourceReliability(e.source_reliability),
                         summary=e.evidence_summary, phone_reference_found=e.phone_reference_found,
                         on_profile=e.source_type == EvidenceSourceType.SEARCH_RESULT.value
                         and _same_profile(e.source_url, profile.canonical_url),
                         collected_at=e.collected_at)
            for e in profile.evidence
            if e.source_type in (EvidenceSourceType.SEARCH_RESULT.value, EvidenceSourceType.USER_SUBMISSION.value)
        ]
        outcome = await self.c.verification.verify(
            profile.profile_url, variants=build_variants(investigation.normalized_phone),
            discovery_source=DiscoverySource(profile.discovery_source), prior_evidence=prior,
        )
        await persist_outcome(self.session, self.c, investigation.id, outcome)
        audit.record(self.session, ctx, "profile.verify", "profile", profile.id, status=outcome.status.value)
        await self.session.commit()
        refreshed = await self.profiles.get_owned(profile_id, principal.id)
        if refreshed is None:
            raise NotFound("Profile not found")
        return refreshed

    async def verify_url(self, principal: Principal, url: str, phone: str | None, ctx: AuditContext,
                         ) -> VerificationOutcome:
        principal.require(Permission.PROFILE_VERIFY)
        variants = None
        if phone:
            variants = build_variants(self.c.phone.require_investigable(phone).e164)
        from app.models.enums import EvidenceSourceType, SourceReliability  # noqa: PLC0415
        from app.services.verification.engine import EvidenceItem  # noqa: PLC0415

        submission = EvidenceItem(
            source_url=url[:2048], source_type=EvidenceSourceType.USER_SUBMISSION, source_name="investigator",
            reliability=SourceReliability.INFORMATIONAL, summary="URL submitted by the investigator.",
            phone_reference_found=False, on_profile=False,
        )
        outcome = await self.c.verification.verify(url, variants=variants,
                                                   discovery_source=DiscoverySource.USER_PROVIDED,
                                                   prior_evidence=[submission])
        audit.record(self.session, ctx, "profile.verify_url", "profile_url", None, platform=outcome.platform,
                     status=outcome.status.value, with_number=phone is not None)
        await self.session.commit()
        return outcome

    # ------------------------------------------------- data subject requests
    async def export_user_data(self, principal: Principal, ctx: AuditContext) -> dict[str, Any]:
        principal.require(Permission.SELF_DATA_MANAGE)
        from app.services.reporting.builder import ReportBuilder  # noqa: PLC0415

        builder = ReportBuilder(self.session)
        investigations = []
        for inv_id in await self.investigations.ids_for_owner(principal.id):
            inv = await self.investigations.get(inv_id, owner_id=principal.id)
            if inv is not None:
                investigations.append((await builder.build(inv)).model_dump(mode="json"))
        audit.record(self.session, ctx, "data.export", "user", principal.id, investigations=len(investigations))
        await self.session.commit()
        user = principal.user
        return {
            "exported_at": utcnow().isoformat(),
            "account": {"id": str(user.id), "email": user.email, "role": user.role.name, "status": user.status,
                        "created_at": user.created_at.isoformat(),
                        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None},
            "investigations": investigations,
        }

    async def erase_user_data(self, principal: Principal, ctx: AuditContext, *, include_account: bool) -> int:
        principal.require(Permission.SELF_DATA_MANAGE)
        count = 0
        for inv_id in await self.investigations.ids_for_owner(principal.id):
            inv = await self.investigations.get(inv_id, owner_id=principal.id, for_update=True)
            if inv is not None:
                await self._erase(inv)
                count += 1
        if include_account:
            await self._close_account(principal.user)
        audit.record(self.session, ctx, "data.erase", "user", principal.id, investigations=count,
                     account_closed=include_account)
        await self.session.commit()
        return count

    async def _close_account(self, user: User) -> None:
        now = utcnow()
        await TokenRepository(self.session).revoke_all_for_user(user.id, now)
        await ApiKeyRepository(self.session).revoke_all_for_user(user.id, now)
        link = await TelegramLinkRepository(self.session).by_user(user.id)
        if link is not None:
            await TelegramLinkRepository(self.session).delete(link)
        user.status = UserStatus.DELETED.value
        user.email = f"deleted-{user.id}@erased.invalid"
        user.password_hash = "!erased"
        user.deleted_at = now


def _same_profile(source_url: str, canonical_url: str) -> bool:
    from app.services.discovery.platforms import identify  # noqa: PLC0415

    hit = identify(source_url)
    return hit is not None and hit[1].canonical_url == canonical_url


async def persist_outcome(session: AsyncSession, container: Container, investigation_id: uuid.UUID,
                          outcome: VerificationOutcome) -> DiscoveredProfile:
    """Upsert a profile + evidence for one verification outcome."""
    profiles = ProfileRepository(session)
    canonical = outcome.canonical_url or outcome.input_url
    profile = await profiles.by_canonical(investigation_id, canonical)
    now = utcnow()
    values = {
        "platform": outcome.platform or "unknown",
        "profile_url": outcome.profile_url or outcome.input_url,
        "handle": outcome.handle,
        "discovery_source": outcome.discovery_source.value,
        "evidence_description": outcome.evidence_description(),
        "verification_status": outcome.status.value,
        "confidence_score": outcome.confidence.score,
        "confidence_level": outcome.confidence.level.value,
        "confidence_factors": outcome.confidence.factors,
        "verification_stages": [s.as_dict() for s in outcome.stages],
        "limitations": list(dict.fromkeys(outcome.limitations)),
        "verified_at": outcome.checked_at,
    }
    if profile is None:
        profile = await profiles.add(DiscoveredProfile(investigation_id=investigation_id, canonical_url=canonical,
                                                       discovered_at=now, **values))
    else:
        for key, value in values.items():
            setattr(profile, key, value)
    expiry = now + timedelta(days=container.settings.retention_evidence_days)
    await profiles.replace_evidence(profile, [
        EvidenceSource(
            source_url=item.source_url[:2048],
            source_type=item.source_type.value,
            source_name=item.source_name[:64],
            source_reliability=item.reliability.value,
            evidence_summary=item.summary,
            phone_reference_found=item.phone_reference_found,
            collected_at=item.collected_at,
            expiration_at=expiry,
        )
        for item in outcome.evidence
    ])
    return profile
