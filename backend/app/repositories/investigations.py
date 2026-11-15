from __future__ import annotations

import builtins
import uuid
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import DiscoveredProfile, EvidenceSource, Investigation, InvestigationJob, PhoneIntelligence
from app.models.enums import ACTIVE_JOB_STATUSES
from app.repositories.base import Page, apply_sort, paginate


class InvestigationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, investigation: Investigation) -> Investigation:
        self.session.add(investigation)
        await self.session.flush()
        return investigation

    async def get(self, investigation_id: uuid.UUID, *, owner_id: uuid.UUID | None,
                  include_deleted: bool = False, for_update: bool = False) -> Investigation | None:
        """Fetch an investigation.  ``owner_id=None`` means unrestricted (privileged callers only)."""
        stmt = select(Investigation).where(Investigation.id == investigation_id).options(
            selectinload(Investigation.phone_intelligence)
        )
        if owner_id is not None:
            stmt = stmt.where(Investigation.user_id == owner_id)
        if not include_deleted:
            stmt = stmt.where(Investigation.deleted_at.is_(None))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        *,
        owner_id: uuid.UUID | None,
        status: str | None,
        authorization_basis: str | None,
        phone_hash: str | None,
        reference: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        sort: str | None,
        page: int,
        page_size: int,
    ) -> Page[Investigation]:
        stmt = select(Investigation).where(Investigation.deleted_at.is_(None)).options(
            selectinload(Investigation.phone_intelligence)
        )
        if owner_id is not None:
            stmt = stmt.where(Investigation.user_id == owner_id)
        if status:
            stmt = stmt.where(Investigation.status == status)
        if authorization_basis:
            stmt = stmt.where(Investigation.authorization_basis == authorization_basis)
        if phone_hash:
            stmt = stmt.where(Investigation.phone_lookup_hash == phone_hash)
        if reference:
            stmt = stmt.where(Investigation.investigation_reference == reference.upper())
        if created_from:
            stmt = stmt.where(Investigation.created_at >= created_from)
        if created_to:
            stmt = stmt.where(Investigation.created_at <= created_to)
        stmt = apply_sort(stmt, sort, {
            "created_at": Investigation.created_at, "updated_at": Investigation.updated_at,
            "status": Investigation.status, "reference": Investigation.investigation_reference,
            "completed_at": Investigation.completed_at,
        }, "-created_at")
        return await paginate(self.session, stmt, page, page_size)

    async def ids_for_owner(self, owner_id: uuid.UUID) -> builtins.list[uuid.UUID]:
        stmt = select(Investigation.id).where(Investigation.user_id == owner_id, Investigation.deleted_at.is_(None))
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_phone_intelligence(self, intel: PhoneIntelligence) -> None:
        self.session.add(intel)
        await self.session.flush()

    async def erase_content(self, investigation: Investigation, now: datetime) -> None:
        """Soft-delete + immediate erasure of all personal/evidence content."""
        await self.session.execute(delete(DiscoveredProfile).where(
            DiscoveredProfile.investigation_id == investigation.id))
        await self.session.execute(delete(PhoneIntelligence).where(
            PhoneIntelligence.investigation_id == investigation.id))
        investigation.normalized_phone = None
        investigation.phone_lookup_hash = None
        investigation.purpose = None
        investigation.authorization_reference = None
        investigation.summary = {}
        investigation.deleted_at = now
        await self.session.flush()


class ProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_canonical(self, investigation_id: uuid.UUID, canonical_url: str) -> DiscoveredProfile | None:
        stmt = select(DiscoveredProfile).where(
            DiscoveredProfile.investigation_id == investigation_id,
            DiscoveredProfile.canonical_url == canonical_url,
        ).options(selectinload(DiscoveredProfile.evidence))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_owned(self, profile_id: uuid.UUID, owner_id: uuid.UUID | None) -> DiscoveredProfile | None:
        stmt = (
            select(DiscoveredProfile)
            .join(Investigation)
            .where(DiscoveredProfile.id == profile_id, Investigation.deleted_at.is_(None))
            .options(selectinload(DiscoveredProfile.evidence))
        )
        if owner_id is not None:
            stmt = stmt.where(Investigation.user_id == owner_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, investigation_id: uuid.UUID, *, platform: str | None, status: str | None,
                   min_confidence: int | None, sort: str | None, page: int, page_size: int,
                   ) -> Page[DiscoveredProfile]:
        stmt = select(DiscoveredProfile).where(DiscoveredProfile.investigation_id == investigation_id).options(
            selectinload(DiscoveredProfile.evidence))
        if platform:
            stmt = stmt.where(DiscoveredProfile.platform == platform)
        if status:
            stmt = stmt.where(DiscoveredProfile.verification_status == status)
        if min_confidence is not None:
            stmt = stmt.where(DiscoveredProfile.confidence_score >= min_confidence)
        stmt = apply_sort(stmt, sort, {
            "confidence": DiscoveredProfile.confidence_score, "discovered_at": DiscoveredProfile.discovered_at,
            "platform": DiscoveredProfile.platform, "status": DiscoveredProfile.verification_status,
        }, "-confidence")
        return await paginate(self.session, stmt, page, page_size)

    async def all_for(self, investigation_id: uuid.UUID) -> builtins.list[DiscoveredProfile]:
        stmt = (select(DiscoveredProfile).where(DiscoveredProfile.investigation_id == investigation_id)
                .options(selectinload(DiscoveredProfile.evidence))
                .order_by(DiscoveredProfile.confidence_score.desc(), DiscoveredProfile.platform))
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(self, profile: DiscoveredProfile) -> DiscoveredProfile:
        self.session.add(profile)
        await self.session.flush()
        return profile

    async def replace_evidence(self, profile: DiscoveredProfile, evidence: builtins.list[EvidenceSource]) -> None:
        await self.session.execute(delete(EvidenceSource).where(EvidenceSource.discovered_profile_id == profile.id))
        for item in evidence:
            item.discovered_profile_id = profile.id
            self.session.add(item)
        await self.session.flush()


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, job: InvestigationJob) -> InvestigationJob:
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(self, job_id: uuid.UUID, *, for_update: bool = False) -> InvestigationJob | None:
        stmt = select(InvestigationJob).where(InvestigationJob.id == job_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active_for(self, investigation_id: uuid.UUID, job_type: str) -> InvestigationJob | None:
        stmt = select(InvestigationJob).where(
            InvestigationJob.investigation_id == investigation_id,
            InvestigationJob.job_type == job_type,
            InvestigationJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def by_idempotency_key(self, investigation_id: uuid.UUID, key: str) -> InvestigationJob | None:
        stmt = select(InvestigationJob).where(
            InvestigationJob.investigation_id == investigation_id, InvestigationJob.idempotency_key == key
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def latest_for(self, investigation_id: uuid.UUID) -> InvestigationJob | None:
        stmt = (select(InvestigationJob).where(InvestigationJob.investigation_id == investigation_id)
                .order_by(InvestigationJob.created_at.desc()).limit(1))
        return (await self.session.execute(stmt)).scalars().first()

    async def cancel_requested(self, job_id: uuid.UUID) -> bool:
        stmt = select(InvestigationJob.cancel_requested_at).where(InvestigationJob.id == job_id)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def mark_cancel_requested_for(self, investigation_id: uuid.UUID, now: datetime) -> list[uuid.UUID]:
        stmt = select(InvestigationJob.id).where(
            InvestigationJob.investigation_id == investigation_id,
            InvestigationJob.status.in_([s.value for s in ACTIVE_JOB_STATUSES]),
        )
        ids = list((await self.session.execute(stmt)).scalars().all())
        if ids:
            await self.session.execute(update(InvestigationJob).where(InvestigationJob.id.in_(ids))
                                       .values(cancel_requested_at=now))
        return ids
