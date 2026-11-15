"""Retention enforcement (run daily by the worker cron or ``python -m app.manage purge``)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import delete, or_, select

from app.core.config import Settings
from app.core.security import utcnow
from app.db.session import transaction
from app.models import AuditLog, EvidenceSource, Investigation, RefreshToken
from app.repositories.investigations import InvestigationRepository
from app.services.audit import AuditContext, record

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PurgeReport:
    investigations_expired: int = 0
    tombstones_removed: int = 0
    evidence_removed: int = 0
    refresh_tokens_removed: int = 0
    audit_logs_removed: int = 0


async def purge_expired(sessionmaker, settings: Settings) -> PurgeReport:  # type: ignore[no-untyped-def]
    report = PurgeReport()
    now = utcnow()
    async with transaction(sessionmaker) as session:
        repo = InvestigationRepository(session)
        expired = (await session.execute(
            select(Investigation).where(Investigation.deleted_at.is_(None), Investigation.expires_at <= now)
        )).scalars().all()
        for inv in expired:
            await repo.erase_content(inv, now)
        report.investigations_expired = len(expired)

        tombstone_cutoff = now - timedelta(days=settings.deleted_tombstone_days)
        result = await session.execute(delete(Investigation).where(
            Investigation.deleted_at.is_not(None), Investigation.deleted_at <= tombstone_cutoff))
        report.tombstones_removed = result.rowcount or 0  # type: ignore[attr-defined]

        result = await session.execute(delete(EvidenceSource).where(EvidenceSource.expiration_at <= now))
        report.evidence_removed = result.rowcount or 0  # type: ignore[attr-defined]

        result = await session.execute(delete(RefreshToken).where(or_(
            RefreshToken.expires_at <= now,
            RefreshToken.revoked_at <= now - timedelta(days=7),
        )))
        report.refresh_tokens_removed = result.rowcount or 0  # type: ignore[attr-defined]

        audit_cutoff = now - timedelta(days=settings.retention_audit_days)
        result = await session.execute(delete(AuditLog).where(AuditLog.timestamp <= audit_cutoff))
        report.audit_logs_removed = result.rowcount or 0  # type: ignore[attr-defined]

        record(session, AuditContext(None, None, "system"), "retention.purge", "system", None, **asdict(report))
    logger.info("retention purge complete", extra=asdict(report))
    return report
