"""Profile URL verification engine.

Ten explicit stages; each one is recorded so results are auditable:

1. url_syntax            6. canonical_url
2. domain_allowlist      7. public_metadata
3. platform              8. evidence_collection
4. availability          9. confidence
5. redirect_inspection  10. final_result

A profile is never attributed to a number because of name similarity.  Only an
explicit public reference to the investigated number counts as evidence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.errors import OutboundBlocked
from app.integrations.http import RedirectBlocked, SafeHttpClient, domain_matches, validate_url_syntax
from app.integrations.platforms.clients import PlatformApiClient, PlatformLookup
from app.models.enums import (
    ConfidenceLevel,
    DiscoverySource,
    EvidenceSourceType,
    SourceReliability,
    VerificationStatus,
)
from app.services.discovery.platforms import ALL_DOMAINS, FetchPolicy, Platform, ProfileRef, identify
from app.services.evidence.redaction import redact_excerpt
from app.services.phone.matching import NumberVariants, text_references_number
from app.services.verification import confidence
from app.services.verification.html_meta import parse_metadata
from app.services.verification.robots import RobotsPolicy

logger = logging.getLogger(__name__)

_LOGIN_MARKERS = ("/login", "/accounts/login", "/signin", "/auth/", "/checkpoint", "login.php")
_PRIVATE_KINDS = frozenset({"invite", "group_invite"})


@dataclass(slots=True)
class EvidenceItem:
    source_url: str
    source_type: EvidenceSourceType
    source_name: str
    reliability: SourceReliability
    summary: str
    phone_reference_found: bool
    on_profile: bool
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "reliability": self.reliability.value,
            "summary": self.summary,
            "phone_reference_found": self.phone_reference_found,
            "collected_at": self.collected_at.isoformat(),
        }


@dataclass(slots=True)
class StageResult:
    stage: str
    outcome: str  # passed | failed | skipped | info
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"stage": self.stage, "outcome": self.outcome, "detail": self.detail}


@dataclass(slots=True)
class VerificationOutcome:
    input_url: str
    discovery_source: DiscoverySource
    status: VerificationStatus
    confidence: confidence.ConfidenceResult
    platform: str | None = None
    platform_name: str | None = None
    kind: str | None = None
    handle: str | None = None
    profile_url: str | None = None
    canonical_url: str | None = None
    display_name: str | None = None
    http_status: int | None = None
    redirect_chain: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    stages: list[StageResult] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def evidence_description(self) -> str:
        phone_hits = [e for e in self.evidence if e.phone_reference_found]
        if self.status is VerificationStatus.VERIFIED_PUBLIC_REFERENCE:
            return "The investigated number is published in this profile's own public content."
        if self.status is VerificationStatus.PUBLIC_CANDIDATE:
            kinds = sorted({e.source_type.value for e in phone_hits})
            return ("Public sources link this profile URL to the investigated number "
                    f"({', '.join(kinds)}), but the profile itself was not live-confirmed to publish it.")
        if self.discovery_source is DiscoverySource.GENERATED_OFFICIAL_LINK:
            return ("Official link format generated from the number. It does not indicate that an account "
                    "exists or that the number is registered.")
        if self.status is VerificationStatus.NOT_FOUND:
            return "The platform reports that this profile does not exist."
        if self.status is VerificationStatus.RESTRICTED:
            return "The profile is restricted (private, login-walled or blocked by policy)."
        if self.status is VerificationStatus.INACCESSIBLE:
            return "The profile could not be reached for verification."
        if self.status is VerificationStatus.ERROR:
            failed = next((s.detail for s in self.stages if s.outcome == "failed"), "verification error")
            return f"Verification failed: {failed}"
        return "No public evidence links this profile to the investigated number."


class VerificationEngine:
    def __init__(
        self,
        http: SafeHttpClient,
        robots: RobotsPolicy,
        api_clients: dict[str, PlatformApiClient],
        *,
        respect_robots: bool = True,
    ) -> None:
        self._http = http
        self._robots = robots
        self._api = api_clients
        self._respect_robots = respect_robots

    # ------------------------------------------------------------------ public
    def generated_link(self, platform: Platform, ref: ProfileRef) -> VerificationOutcome:
        evidence = EvidenceItem(
            source_url=ref.canonical_url,
            source_type=EvidenceSourceType.GENERATED_LINK,
            source_name="phoneintel",
            reliability=SourceReliability.INFORMATIONAL,
            summary="Official click-to-chat URL generated from the number format.",
            phone_reference_found=False,
            on_profile=False,
        )
        return VerificationOutcome(
            input_url=ref.canonical_url,
            discovery_source=DiscoverySource.GENERATED_OFFICIAL_LINK,
            status=VerificationStatus.UNVERIFIED,
            confidence=confidence.ConfidenceResult(score=0, level=ConfidenceLevel.NONE, factors=[]),
            platform=platform.key, platform_name=platform.name, kind=ref.kind, handle=ref.handle,
            profile_url=ref.canonical_url, canonical_url=ref.canonical_url,
            evidence=[evidence],
            stages=[StageResult("final_result", "info", "Generated link; no availability check performed")],
            limitations=[platform.limitation],
        )

    async def verify(
        self,
        url: str,
        *,
        variants: NumberVariants | None,
        discovery_source: DiscoverySource,
        prior_evidence: list[EvidenceItem] | None = None,
        live_check: bool = True,
    ) -> VerificationOutcome:
        stages: list[StageResult] = []
        limitations: list[str] = []
        evidence = list(prior_evidence or [])

        def fail(stage: str, detail: str) -> VerificationOutcome:
            stages.append(StageResult(stage, "failed", detail))
            stages.append(StageResult("final_result", "info", VerificationStatus.ERROR.value))
            return VerificationOutcome(
                input_url=url[:2048], discovery_source=discovery_source, status=VerificationStatus.ERROR,
                confidence=confidence.ConfidenceResult(0, ConfidenceLevel.NONE, []), stages=stages,
                limitations=limitations,
            )

        # 1. URL syntax ------------------------------------------------------
        try:
            validated = validate_url_syntax(url, allowed_domains=None, allow_http=True)
        except OutboundBlocked as exc:
            return fail("url_syntax", exc.message)
        stages.append(StageResult("url_syntax", "passed", "Well-formed http(s) URL without credentials"))

        # 2. Domain allowlist ------------------------------------------------
        if not domain_matches(validated.host, ALL_DOMAINS):
            return fail("domain_allowlist", "Domain is not a supported social platform")
        stages.append(StageResult("domain_allowlist", "passed", f"{validated.host} is allowlisted"))

        # 3. Platform identification -----------------------------------------
        hit = identify(validated.url)
        if hit is None:
            return fail("platform", "URL is not a recognised public profile/channel URL")
        platform, ref = hit
        stages.append(StageResult("platform", "passed", f"{platform.name} {ref.kind}"))
        limitations.append(platform.limitation)
        limitations.extend(platform.extra_limitations)

        outcome = VerificationOutcome(
            input_url=url[:2048], discovery_source=discovery_source, status=VerificationStatus.UNVERIFIED,
            confidence=confidence.ConfidenceResult(0, ConfidenceLevel.NONE, []), platform=platform.key,
            platform_name=platform.name, kind=ref.kind, handle=ref.handle,
            profile_url=ref.canonical_url, canonical_url=ref.canonical_url, stages=stages,
            limitations=limitations, evidence=evidence,
        )

        # 4-7. Availability, redirects, canonical URL, metadata ---------------
        live_state = "skipped"
        via_api = False
        if not live_check:
            stages.append(StageResult("availability", "skipped", "Live checks disabled for this run"))
        elif ref.kind in _PRIVATE_KINDS:
            stages.append(StageResult("availability", "skipped", "Private invite links are never opened"))
        else:
            live_state, via_api = await self._live_check(platform, ref, variants, outcome)

        # 8. Evidence collection --------------------------------------------
        stages.append(StageResult("evidence_collection", "passed" if evidence else "info",
                                  f"{len(evidence)} evidence item(s), "
                                  f"{sum(e.phone_reference_found for e in evidence)} with a number reference"))

        # 9. Confidence -----------------------------------------------------
        active = self._factors(evidence, live_state, via_api, discovery_source)
        outcome.confidence = confidence.score(active)
        stages.append(StageResult("confidence", "info",
                                  f"{outcome.confidence.level.value} ({outcome.confidence.score}/100)"))

        # 10. Final status --------------------------------------------------
        outcome.status = self._status(evidence, live_state)
        stages.append(StageResult("final_result", "info", outcome.status.value))
        return outcome

    # --------------------------------------------------------------- internals
    async def _live_check(
        self, platform: Platform, ref: ProfileRef, variants: NumberVariants | None, outcome: VerificationOutcome
    ) -> tuple[str, bool]:
        stages = outcome.stages
        client = self._api.get(platform.key)
        if client is not None and ref.handle:
            lookup = await client.lookup(ref.handle, ref.kind)
            state = self._apply_lookup(lookup, variants, outcome)
            if state != "unknown" or platform.fetch_policy is not FetchPolicy.PUBLIC_PAGE:
                return state, state == "exists"
        if platform.fetch_policy is FetchPolicy.PUBLIC_PAGE:
            return await self._public_page(platform, ref, variants, outcome), False
        if platform.fetch_policy is FetchPolicy.OFFICIAL_API and client is None:
            stages.append(StageResult("availability", "skipped",
                                      f"No credentials configured for the official {platform.name} API"))
            outcome.limitations.append(f"{platform.name} official API not configured; existence not checked.")
            return "skipped", False
        if client is None:
            stages.append(StageResult("availability", "skipped", "Live checks not permitted for this platform"))
        return "skipped", False

    def _apply_lookup(self, lookup: PlatformLookup, variants: NumberVariants | None,
                      outcome: VerificationOutcome) -> str:
        stages = outcome.stages
        if lookup.error and lookup.exists is None:
            stages.append(StageResult("availability", "info",
                                      f"{lookup.source_name}: inconclusive ({lookup.error})"))
            if lookup.error.startswith("blocked"):
                return "inaccessible"
            return "unknown"
        outcome.http_status = lookup.status_code
        if lookup.exists is False:
            stages.append(StageResult("availability", "failed", f"{lookup.source_name}: profile not found"))
            return "not_found"
        if lookup.restricted:
            stages.append(StageResult("availability", "failed", f"{lookup.source_name}: profile restricted"))
            return "restricted"
        stages.append(StageResult("availability", "passed", f"{lookup.source_name}: profile exists"))
        stages.append(StageResult("redirect_inspection", "skipped", "Official API response (no redirects)"))
        if lookup.canonical_url:
            hit = identify(lookup.canonical_url)
            if hit is not None and hit[0].key == outcome.platform:
                outcome.canonical_url = hit[1].canonical_url
        stages.append(StageResult("canonical_url", "passed", outcome.canonical_url or ""))
        outcome.display_name = lookup.display_name
        found = variants is not None and any(text_references_number(t, variants) for t in lookup.public_text)
        matched = next((t for t in lookup.public_text if variants and text_references_number(t, variants)), None)
        outcome.evidence.append(EvidenceItem(
            source_url=lookup.api_url,
            source_type=EvidenceSourceType.OFFICIAL_PLATFORM_API,
            source_name=lookup.source_name,
            reliability=SourceReliability.HIGH,
            summary=redact_excerpt(matched, variants) if matched else "Profile exists; public profile fields "
                    "do not contain the investigated number.",
            phone_reference_found=found,
            on_profile=True,
        ))
        stages.append(StageResult("public_metadata", "passed",
                                  "Number referenced in public profile fields" if found
                                  else "No number reference in public profile fields"))
        return "exists"

    async def _public_page(self, platform: Platform, ref: ProfileRef, variants: NumberVariants | None,
                           outcome: VerificationOutcome) -> str:
        stages = outcome.stages
        domains = frozenset(platform.domains)
        if self._respect_robots and not await self._robots.allowed(ref.canonical_url, allowed_domains=domains):
            stages.append(StageResult("availability", "skipped", "Disallowed by the site's robots.txt"))
            outcome.limitations.append("Live check skipped: robots.txt disallows automated access.")
            return "skipped"
        try:
            result = await self._http.fetch(ref.canonical_url, allowed_domains=domains, target_label=platform.key)
        except RedirectBlocked as exc:
            outcome.redirect_chain = [{"url": h.url, "status": h.status_code} for h in exc.chain]
            stages.append(StageResult("availability", "failed", "Redirect refused"))
            stages.append(StageResult("redirect_inspection", "failed",
                                      f"Redirect chain left the platform allowlist ({exc.reason})"))
            return "restricted"
        except OutboundBlocked as exc:
            stages.append(StageResult("availability", "failed", f"Outbound policy: {exc.message}"))
            return "inaccessible"
        except httpx.HTTPError as exc:
            stages.append(StageResult("availability", "failed", f"Network error ({type(exc).__name__})"))
            return "inaccessible"

        outcome.http_status = result.status_code
        outcome.redirect_chain = [{"url": h.url, "status": h.status_code} for h in result.redirect_chain]
        final_path = httpx.URL(result.final_url).path.lower()
        stages.append(StageResult(
            "redirect_inspection", "passed" if not result.redirect_chain else "info",
            f"{len(result.redirect_chain)} redirect(s); final host {httpx.URL(result.final_url).host}",
        ))
        if result.status_code in (404, 410):
            stages.append(StageResult("availability", "failed", f"HTTP {result.status_code}"))
            return "not_found"
        if result.status_code in (401, 403, 451) or any(m in final_path for m in _LOGIN_MARKERS):
            stages.append(StageResult("availability", "failed", "Login wall or access denied"))
            return "restricted"
        if result.status_code == 429:
            outcome.limitations.append("Platform rate-limited the live check.")
            stages.append(StageResult("availability", "failed", "HTTP 429 rate limited"))
            return "inaccessible"
        if result.status_code != 200:
            stages.append(StageResult("availability", "failed", f"HTTP {result.status_code}"))
            return "inaccessible"
        meta = parse_metadata(result.text())
        if platform.key == "telegram" and "tgme_page_title" not in meta.classes:
            stages.append(StageResult("availability", "failed",
                                      "Public preview shows no public profile for this username"))
            outcome.limitations.append("Telegram preview did not show a public profile (may not exist).")
            return "not_found"
        stages.append(StageResult("availability", "passed", f"HTTP 200 ({result.elapsed_ms} ms)"))
        canonical = meta.canonical or meta.og_url
        if canonical:
            hit = identify(canonical)
            if hit is not None and hit[0].key == platform.key:
                outcome.canonical_url = hit[1].canonical_url
        stages.append(StageResult("canonical_url", "passed", outcome.canonical_url or ref.canonical_url))
        outcome.display_name = (meta.og_title or meta.title or "")[:200] or None
        texts = meta.public_text()
        matched = next((t for t in texts if variants and text_references_number(t, variants)), None)
        outcome.evidence.append(EvidenceItem(
            source_url=result.final_url,
            source_type=EvidenceSourceType.LIVE_PROFILE_PAGE,
            source_name=f"{platform.key}_public_page",
            reliability=SourceReliability.HIGH,
            summary=redact_excerpt(matched, variants) if matched
            else "Public page reachable; it does not contain the investigated number.",
            phone_reference_found=matched is not None,
            on_profile=True,
        ))
        stages.append(StageResult("public_metadata", "passed",
                                  "Number referenced on the public page" if matched
                                  else "No number reference on the public page"))
        return "exists"

    @staticmethod
    def _factors(evidence: list[EvidenceItem], live_state: str, via_api: bool,
                 discovery_source: DiscoverySource) -> set[str]:
        active: set[str] = set()
        positive = [e for e in evidence if e.phone_reference_found]
        live_types = (EvidenceSourceType.LIVE_PROFILE_PAGE, EvidenceSourceType.OFFICIAL_PLATFORM_API)
        if any(e.on_profile and e.source_type in live_types for e in positive):
            active.add("phone_on_profile_live")
        if any(e.on_profile and e.source_type is EvidenceSourceType.SEARCH_RESULT for e in positive):
            active.add("phone_on_profile_index")
        if any(not e.on_profile for e in positive):
            active.add("phone_on_third_party_page")
        independent = {(e.source_name, httpx.URL(e.source_url).host) for e in positive}
        if len(independent) >= 2:
            active.add("multiple_independent_sources")
        if live_state == "exists":
            active.add("profile_exists_confirmed")
            if via_api:
                active.add("official_api_confirmation")
        elif live_state == "not_found":
            active.add("profile_not_found")
        elif live_state == "restricted":
            active.add("profile_restricted")
        if discovery_source is DiscoverySource.USER_PROVIDED:
            active.add("user_provided_link")
        return active

    @staticmethod
    def _status(evidence: list[EvidenceItem], live_state: str) -> VerificationStatus:
        positive = [e for e in evidence if e.phone_reference_found]
        live_types = (EvidenceSourceType.LIVE_PROFILE_PAGE, EvidenceSourceType.OFFICIAL_PLATFORM_API)
        if live_state == "not_found":
            return VerificationStatus.NOT_FOUND
        if any(e.on_profile and e.source_type in live_types for e in positive):
            return VerificationStatus.VERIFIED_PUBLIC_REFERENCE
        if positive:
            return VerificationStatus.PUBLIC_CANDIDATE
        if live_state == "restricted":
            return VerificationStatus.RESTRICTED
        if live_state == "inaccessible":
            return VerificationStatus.INACCESSIBLE
        return VerificationStatus.UNVERIFIED
