"""Discovery job execution (shared by the arq worker and the inline queue).

Lifecycle: queued -> running -> succeeded | cancelled | retrying -> ... -> dead_letter.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.metrics import DISCOVERY_JOBS, PROFILES_DISCOVERED
from app.core.security import utcnow
from app.db.session import transaction
from app.models.enums import (
    DiscoverySource,
    EvidenceSourceType,
    InvestigationStatus,
    JobStatus,
    SourceReliability,
    VerificationStatus,
)
from app.repositories.investigations import InvestigationRepository, JobRepository
from app.services.audit import AuditContext, record
from app.services.discovery.platforms import PLATFORMS, whatsapp_click_to_chat
from app.services.discovery.service import Candidate, DiscoveryCancelled, SearchRunResult
from app.services.events import EventType
from app.services.investigations import persist_outcome
from app.services.phone.matching import NumberVariants, build_variants
from app.services.verification.engine import EvidenceItem, VerificationOutcome

if TYPE_CHECKING:
    from app.services.container import Container

logger = logging.getLogger(__name__)

DLQ_KEY = "pi:jobs:dead_letter"


@dataclass(frozen=True, slots=True)
class JobOutcome:
    status: JobStatus
    retry_in: float | None = None


class TransientJobError(Exception):
    """Errors worth retrying (network/provider/database hiccups)."""


class DiscoveryJobRunner:
    def __init__(self, container: Container) -> None:
        self.c = container

    def backoff(self, attempt: int) -> float:
        return float(min(self.c.settings.job_backoff_base_seconds * (2 ** (attempt - 1)), 600))

    async def run(self, job_id: uuid.UUID, attempt: int = 1) -> JobOutcome:
        try:
            async with asyncio.timeout(self.c.settings.job_timeout_seconds):
                return await self._run(job_id, attempt)
        except DiscoveryCancelled:
            await self._finish_cancelled(job_id)
            return JobOutcome(JobStatus.CANCELLED)
        except TimeoutError:
            return await self._failure(job_id, attempt, "timeout", "Job exceeded its time budget")
        except Exception as exc:
            logger.exception("discovery job failed", extra={"job_id": str(job_id), "attempt": attempt})
            return await self._failure(job_id, attempt, type(exc).__name__[:64], "Unexpected processing error")

    # ----------------------------------------------------------------- core
    async def _run(self, job_id: uuid.UUID, attempt: int) -> JobOutcome:
        sm = self.c.sessionmaker
        async with transaction(sm) as session:
            jobs = JobRepository(session)
            job = await jobs.get(job_id, for_update=True)
            if job is None:
                logger.warning("job vanished before execution", extra={"job_id": str(job_id)})
                return JobOutcome(JobStatus.CANCELLED)
            if job.status in (
                JobStatus.SUCCEEDED.value,
                JobStatus.CANCELLED.value,
                JobStatus.DEAD_LETTER.value,
                JobStatus.FAILED.value,
            ):
                return JobOutcome(JobStatus(job.status))
            if job.cancel_requested_at is not None:
                raise DiscoveryCancelled
            inv = await InvestigationRepository(session).get(job.investigation_id, owner_id=None, for_update=True)
            if inv is None or inv.normalized_phone is None:
                raise DiscoveryCancelled
            job.status = JobStatus.RUNNING.value
            job.retry_count = attempt - 1
            job.started_at = job.started_at or utcnow()
            inv.status = InvestigationStatus.RUNNING.value
            inv.started_at = inv.started_at or utcnow()
            params: dict[str, Any] = dict(job.parameters)
            investigation_id, user_id, e164 = inv.id, inv.user_id, inv.normalized_phone

        events = self.c.events
        if attempt == 1:
            await events.publish(investigation_id, EventType.INVESTIGATION_STARTED, {"job_id": str(job_id)})
        await events.publish(
            investigation_id,
            EventType.DISCOVERY_STARTED,
            {
                "job_id": str(job_id),
                "attempt": attempt,
                "platforms": params["platforms"],
                "web_search": params["include_web_search"],
            },
        )

        async def cancelled() -> bool:
            async with sm() as s:
                return await JobRepository(s).cancel_requested(job_id)

        variants = build_variants(e164)
        platforms: list[str] = params["platforms"]
        live = bool(params.get("live_checks", True))
        candidates: dict[str, Candidate] = {}
        outcomes: list[VerificationOutcome] = []

        # 1) Investigator-supplied URLs.
        for url in params.get("profile_urls", []):
            outcome = await self.c.verification.verify(
                url,
                variants=variants,
                discovery_source=DiscoverySource.USER_PROVIDED,
                live_check=live,
                prior_evidence=[
                    EvidenceItem(
                        source_url=url[:2048],
                        source_type=EvidenceSourceType.USER_SUBMISSION,
                        source_name="investigator",
                        reliability=SourceReliability.INFORMATIONAL,
                        summary="URL submitted by the investigator.",
                        phone_reference_found=False,
                        on_profile=False,
                    )
                ],
            )
            outcomes.append(outcome)
            await self._store(investigation_id, outcome)

        # 2) Official link formats generated from the number (format only, no registration checks).
        if "whatsapp" in platforms:
            outcome = self.c.verification.generated_link(PLATFORMS["whatsapp"], whatsapp_click_to_chat(e164))
            outcomes.append(outcome)
            await self._store(investigation_id, outcome)

        # 3) Public web search.
        search = SearchRunResult()
        if params.get("include_web_search", True):

            async def progress(data: dict[str, object]) -> None:
                await events.publish(investigation_id, EventType.DISCOVERY_PROGRESS, {"phase": "search", **data})

            search = await self.c.discovery.search(variants, platforms, on_progress=progress, is_cancelled=cancelled)
            candidates = search.candidates
            for url in {o.canonical_url for o in outcomes if o.canonical_url}:
                candidates.pop(url, None)

        # 4) Verify each candidate (bounded concurrency, cooperative cancellation).
        semaphore = asyncio.Semaphore(self.c.settings.discovery_max_concurrency)
        total = len(candidates)
        done = 0

        async def verify(candidate: Candidate) -> None:
            nonlocal done
            async with semaphore:
                if await cancelled():
                    raise DiscoveryCancelled
                outcome = await self.c.verification.verify(
                    candidate.ref.canonical_url,
                    variants=variants,
                    discovery_source=candidate.discovery_source,
                    prior_evidence=candidate.evidence,
                    live_check=live,
                )
                outcomes.append(outcome)
                await self._store(investigation_id, outcome)
                done += 1
                await events.publish(
                    investigation_id,
                    EventType.DISCOVERY_PROGRESS,
                    {"phase": "verification", "verified": done, "total": total},
                )

        await asyncio.gather(*(verify(c) for c in candidates.values()))
        if await cancelled():
            raise DiscoveryCancelled

        # 5) Summary + completion.
        summary = self._summary(outcomes, search, variants, platforms)
        async with transaction(sm) as session:
            job = await JobRepository(session).get(job_id, for_update=True)
            inv = await InvestigationRepository(session).get(investigation_id, owner_id=None, for_update=True)
            if job is None or inv is None:
                raise DiscoveryCancelled
            job.status = JobStatus.SUCCEEDED.value
            job.completed_at = utcnow()
            job.progress = {"verified": len(outcomes), "queries_executed": search.queries_executed}
            job.error_code = None
            inv.status = InvestigationStatus.COMPLETED.value
            inv.completed_at = utcnow()
            inv.summary = summary
            record(
                session,
                AuditContext(user_id, None, "worker"),
                "discovery.completed",
                "investigation",
                investigation_id,
                job_id=str(job_id),
                profiles=len(outcomes),
            )
        DISCOVERY_JOBS.labels(status="succeeded").inc()
        await events.publish(
            investigation_id, EventType.INVESTIGATION_COMPLETED, {"job_id": str(job_id), "summary": summary["counts"]}
        )
        return JobOutcome(JobStatus.SUCCEEDED)

    async def _store(self, investigation_id: uuid.UUID, outcome: VerificationOutcome) -> None:
        async with transaction(self.c.sessionmaker) as session:
            profile = await persist_outcome(session, self.c, investigation_id, outcome)
            profile_id = profile.id
        PROFILES_DISCOVERED.labels(platform=outcome.platform or "unknown", status=outcome.status.value).inc()
        common = {"profile_id": str(profile_id), "platform": outcome.platform, "url": outcome.canonical_url}
        await self.c.events.publish(
            investigation_id, EventType.PROFILE_DISCOVERED, {**common, "source": outcome.discovery_source.value}
        )
        await self.c.events.publish(
            investigation_id,
            EventType.PROFILE_VERIFIED,
            {
                **common,
                "status": outcome.status.value,
                "confidence": outcome.confidence.level.value,
                "score": outcome.confidence.score,
            },
        )

    @staticmethod
    def _summary(
        outcomes: list[VerificationOutcome], search: SearchRunResult, variants: NumberVariants, platforms: list[str]
    ) -> dict[str, Any]:
        counts = {status.value: 0 for status in VerificationStatus}
        for o in outcomes:
            counts[o.status.value] += 1
        limitations = [
            "A matching public reference indicates the number was published alongside the profile; it does not "
            "prove the number holder controls the profile.",
            "Name or username similarity is never used as evidence.",
            "Only public sources, official APIs and investigator-supplied links were used; private accounts, "
            "hidden registrations and access-controlled content are out of scope by design.",
        ]
        if not search.providers_used:
            limitations.append("No public search provider is configured; web discovery was not performed.")
        if search.provider_errors:
            limitations.append(f"{len(search.provider_errors)} search request(s) failed; results may be incomplete.")
        platform_limits = sorted({PLATFORMS[p].limitation for p in platforms if p in PLATFORMS})
        return {
            "counts": {"total_profiles": len(outcomes), **counts},
            "search": {
                "providers": search.providers_used,
                "queries_planned": search.queries_planned,
                "queries_executed": search.queries_executed,
                "results_seen": search.results_seen,
                "unique_results": search.unique_results,
                "web_mentions_without_profile": search.web_mentions_without_profile,
                "provider_errors": search.provider_errors,
            },
            "platforms": platforms,
            "limitations": limitations + platform_limits,
        }

    # ------------------------------------------------------------- failures
    async def _finish_cancelled(self, job_id: uuid.UUID) -> None:
        investigation_id = None
        async with transaction(self.c.sessionmaker) as session:
            job = await JobRepository(session).get(job_id, for_update=True)
            if job is None:
                return
            job.status = JobStatus.CANCELLED.value
            job.completed_at = utcnow()
            inv = await InvestigationRepository(session).get(
                job.investigation_id, owner_id=None, include_deleted=True, for_update=True
            )
            if inv is not None and inv.status not in (InvestigationStatus.COMPLETED.value,):
                inv.status = InvestigationStatus.CANCELLED.value
                investigation_id = inv.id
        DISCOVERY_JOBS.labels(status="cancelled").inc()
        if investigation_id is not None:
            await self.c.events.publish(investigation_id, EventType.INVESTIGATION_CANCELLED, {"job_id": str(job_id)})

    async def _failure(self, job_id: uuid.UUID, attempt: int, code: str, detail: str) -> JobOutcome:
        final = attempt >= self.c.settings.job_max_tries
        investigation_id = None
        async with transaction(self.c.sessionmaker) as session:
            job = await JobRepository(session).get(job_id, for_update=True)
            if job is None:
                return JobOutcome(JobStatus.FAILED)
            job.retry_count = attempt
            job.error_code = code
            job.error_detail = detail
            investigation_id = job.investigation_id
            inv = await InvestigationRepository(session).get(job.investigation_id, owner_id=None, for_update=True)
            if final:
                job.status = JobStatus.DEAD_LETTER.value
                job.completed_at = utcnow()
                if inv is not None:
                    inv.status = InvestigationStatus.FAILED.value
            else:
                job.status = JobStatus.RETRYING.value
        if final:
            DISCOVERY_JOBS.labels(status="dead_letter").inc()
            await self.c.redis.rpush(DLQ_KEY, str(job_id))  # type: ignore[misc]
            await self.c.events.publish(
                investigation_id,
                EventType.INVESTIGATION_FAILED,
                {"job_id": str(job_id), "error_code": code, "attempts": attempt},
            )
            return JobOutcome(JobStatus.DEAD_LETTER)
        DISCOVERY_JOBS.labels(status="retrying").inc()
        delay = self.backoff(attempt)
        await self.c.events.publish(
            investigation_id,
            EventType.DISCOVERY_PROGRESS,
            {"phase": "retry_scheduled", "attempt": attempt, "retry_in_seconds": delay, "error_code": code},
        )
        return JobOutcome(JobStatus.RETRYING, retry_in=delay)
