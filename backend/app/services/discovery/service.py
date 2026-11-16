"""Public-source discovery: query building, provider fan-out, deduplication and
candidate extraction.  Verification of each candidate happens afterwards in
:class:`~app.services.verification.engine.VerificationEngine`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

import httpx

from app.integrations.search.providers import SearchHit, SearchProvider, SearchProviderError
from app.models.enums import DiscoverySource, EvidenceSourceType, SourceReliability
from app.services.discovery.platforms import PLATFORMS, Platform, ProfileRef, extract_profile_urls, identify
from app.services.evidence.redaction import redact_excerpt
from app.services.phone.matching import NumberVariants, text_references_number
from app.services.verification.engine import EvidenceItem

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, object]], Awaitable[None]]
CancelCheck = Callable[[], Awaitable[bool]]


class DiscoveryCancelled(Exception):
    pass


@dataclass(slots=True)
class Candidate:
    platform: Platform
    ref: ProfileRef
    discovery_source: DiscoverySource
    original_url: str
    evidence: list[EvidenceItem] = field(default_factory=list)


@dataclass(slots=True)
class SearchRunResult:
    candidates: dict[str, Candidate] = field(default_factory=dict)
    queries_planned: int = 0
    queries_executed: int = 0
    results_seen: int = 0
    unique_results: int = 0
    web_mentions_without_profile: int = 0
    provider_errors: list[dict[str, str]] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)


def normalize_result_url(url: str) -> str:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return url
    host = (parsed.host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return f"{host}{path}?{parsed.query.decode()}" if parsed.query else f"{host}{path}"


class DiscoveryService:
    def __init__(
        self,
        providers: list[SearchProvider],
        *,
        max_queries: int = 12,
        results_per_query: int = 10,
        concurrency: int = 4,
    ) -> None:
        self.providers = providers
        self._max_queries = max_queries
        self._per_query = results_per_query
        self._concurrency = max(1, concurrency)

    def build_queries(self, variants: NumberVariants, platforms: Iterable[str]) -> list[str]:
        quoted = " OR ".join(f'"{form}"' for form in variants.query_forms)
        queries = [quoted]
        for key in platforms:
            platform = PLATFORMS.get(key)
            if platform is None:
                continue
            sites = " OR ".join(f"site:{s}" for s in platform.search_sites)
            queries.append(f"({quoted}) ({sites})")
        return list(dict.fromkeys(queries))[: self._max_queries]

    async def search(
        self,
        variants: NumberVariants,
        platforms: list[str],
        *,
        on_progress: ProgressCallback | None = None,
        is_cancelled: CancelCheck | None = None,
    ) -> SearchRunResult:
        run = SearchRunResult(providers_used=[p.name for p in self.providers])
        if not self.providers:
            return run
        queries = self.build_queries(variants, platforms)
        tasks = [(provider, query) for query in queries for provider in self.providers]
        run.queries_planned = len(tasks)
        semaphore = asyncio.Semaphore(self._concurrency)
        seen: set[str] = set()
        allowed = set(platforms)
        lock = asyncio.Lock()

        async def execute(provider: SearchProvider, query: str) -> None:
            async with semaphore:
                if is_cancelled is not None and await is_cancelled():
                    raise DiscoveryCancelled
                try:
                    hits = await provider.search(query, count=self._per_query)
                except SearchProviderError as exc:
                    logger.warning("search provider error", extra={"provider": exc.provider, "code": exc.code})
                    async with lock:
                        run.provider_errors.append({"provider": exc.provider, "code": exc.code})
                    return
                async with lock:
                    run.queries_executed += 1
                    run.results_seen += len(hits)
                    for hit in hits:
                        key = normalize_result_url(hit.url)
                        if key in seen:
                            self._merge_duplicate(run, hit, variants, allowed)
                            continue
                        seen.add(key)
                        run.unique_results += 1
                        self._ingest(run, hit, variants, allowed)
                    progress = {
                        "queries_executed": run.queries_executed,
                        "queries_planned": run.queries_planned,
                        "candidates": len(run.candidates),
                        "provider": provider.name,
                    }
                if on_progress is not None:
                    await on_progress(progress)

        results = await asyncio.gather(*(execute(p, q) for p, q in tasks), return_exceptions=True)
        for result in results:
            if isinstance(result, DiscoveryCancelled):
                raise result
            if isinstance(result, BaseException):
                raise result
        return run

    # ------------------------------------------------------------- internals
    def _merge_duplicate(
        self, run: SearchRunResult, hit: SearchHit, variants: NumberVariants, allowed: set[str]
    ) -> None:
        """A second provider returned the same URL: add it as independent evidence."""
        identity = identify(hit.url)
        if identity is None or identity[0].key not in allowed:
            return
        candidate = run.candidates.get(identity[1].canonical_url)
        if candidate is None or any(e.source_name == hit.provider for e in candidate.evidence):
            return
        candidate.evidence.append(self._hit_evidence(hit, variants, on_profile=True))

    def _ingest(self, run: SearchRunResult, hit: SearchHit, variants: NumberVariants, allowed: set[str]) -> None:
        text = f"{hit.title} {hit.snippet}"
        phone_found = text_references_number(text, variants)
        produced = False
        identity = identify(hit.url)
        if identity is not None and identity[0].key in allowed:
            platform, ref = identity
            produced = True
            candidate = run.candidates.setdefault(
                ref.canonical_url,
                Candidate(
                    platform=platform, ref=ref, discovery_source=DiscoverySource.SEARCH_INDEX, original_url=hit.url
                ),
            )
            candidate.evidence.append(self._hit_evidence(hit, variants, on_profile=True, phone_found=phone_found))
        if phone_found:
            for platform, ref in extract_profile_urls(text):
                if platform.key not in allowed or (identity is not None and ref == identity[1]):
                    continue
                produced = True
                candidate = run.candidates.setdefault(
                    ref.canonical_url,
                    Candidate(
                        platform=platform,
                        ref=ref,
                        discovery_source=DiscoverySource.SEARCH_SNIPPET_EXTRACTION,
                        original_url=ref.canonical_url,
                    ),
                )
                candidate.evidence.append(
                    EvidenceItem(
                        source_url=hit.url[:2048],
                        source_type=EvidenceSourceType.SEARCH_RESULT,
                        source_name=hit.provider,
                        reliability=SourceReliability.LOW,
                        summary=redact_excerpt(text, variants),
                        phone_reference_found=True,
                        on_profile=False,
                        collected_at=hit.retrieved_at,
                    )
                )
        if phone_found and not produced:
            run.web_mentions_without_profile += 1

    @staticmethod
    def _hit_evidence(
        hit: SearchHit, variants: NumberVariants, *, on_profile: bool, phone_found: bool | None = None
    ) -> EvidenceItem:
        text = f"{hit.title} {hit.snippet}"
        found = text_references_number(text, variants) if phone_found is None else phone_found
        summary = redact_excerpt(text, variants)
        if not found:
            summary = (
                "Returned for an exact-number query, but the indexed snippet does not show the number. " + summary
            )[:400]
        return EvidenceItem(
            source_url=hit.url[:2048],
            source_type=EvidenceSourceType.SEARCH_RESULT,
            source_name=hit.provider,
            reliability=SourceReliability.MEDIUM,
            summary=summary,
            phone_reference_found=found,
            on_profile=on_profile,
            collected_at=hit.retrieved_at,
        )
