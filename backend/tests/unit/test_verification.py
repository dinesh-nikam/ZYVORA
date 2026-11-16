from __future__ import annotations

import httpx
import pytest
from app.models.enums import (
    ConfidenceLevel,
    DiscoverySource,
    EvidenceSourceType,
    SourceReliability,
    VerificationStatus,
)
from app.services.container import Container
from app.services.phone.matching import build_variants
from app.services.verification.engine import EvidenceItem

from tests.conftest import MockWeb

VARIANTS = build_variants("+919876543210")


def index_evidence(url: str, found: bool, *, on_profile: bool = True, provider: str = "brave") -> EvidenceItem:
    return EvidenceItem(
        source_url=url,
        source_type=EvidenceSourceType.SEARCH_RESULT,
        source_name=provider,
        reliability=SourceReliability.MEDIUM,
        summary="snippet",
        phone_reference_found=found,
        on_profile=on_profile,
    )


async def test_live_public_page_with_number_is_verified(container: Container) -> None:
    out = await container.verification.verify(
        "https://t.me/AcmeHelp", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.VERIFIED_PUBLIC_REFERENCE
    assert out.confidence.level is ConfidenceLevel.HIGH
    assert out.canonical_url == "https://t.me/acmehelp"
    assert out.display_name == "ACME Help Desk"
    stages = [s.stage for s in out.stages]
    for expected in (
        "url_syntax",
        "domain_allowlist",
        "platform",
        "availability",
        "redirect_inspection",
        "canonical_url",
        "public_metadata",
        "evidence_collection",
        "confidence",
        "final_result",
    ):
        assert expected in stages
    live = [e for e in out.evidence if e.source_type is EvidenceSourceType.LIVE_PROFILE_PAGE]
    assert live and live[0].phone_reference_found
    # Unrelated numbers and e-mails in the page are redacted from the stored excerpt.
    assert "022 2345 6789" not in live[0].summary
    assert "[investigated number]" in live[0].summary


async def test_official_api_reference_is_verified(container: Container) -> None:
    out = await container.verification.verify(
        "https://github.com/AcmeDev", variants=VARIANTS, discovery_source=DiscoverySource.SEARCH_INDEX
    )
    assert out.status is VerificationStatus.VERIFIED_PUBLIC_REFERENCE
    assert any(f["factor"] == "official_api_confirmation" for f in out.confidence.factors)


async def test_official_api_not_found(container: Container) -> None:
    out = await container.verification.verify(
        "https://github.com/ghost-user", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.NOT_FOUND
    assert out.confidence.score == 0


async def test_no_live_platform_relies_on_index_evidence(container: Container, web: MockWeb) -> None:
    url = "https://www.instagram.com/acme_support/"
    out = await container.verification.verify(
        url,
        variants=VARIANTS,
        discovery_source=DiscoverySource.SEARCH_INDEX,
        prior_evidence=[index_evidence(url, True)],
    )
    assert out.status is VerificationStatus.PUBLIC_CANDIDATE
    assert out.confidence.level is ConfidenceLevel.LOW or out.confidence.level is ConfidenceLevel.MEDIUM
    assert "instagram.com" not in web.hosts_called()  # never fetched
    assert any("Live profile checks are not performed" in lim for lim in out.limitations)


async def test_name_match_only_is_unverified(container: Container) -> None:
    url = "https://www.facebook.com/john.doe.123"
    out = await container.verification.verify(
        url,
        variants=VARIANTS,
        discovery_source=DiscoverySource.SEARCH_INDEX,
        prior_evidence=[index_evidence(url, False)],
    )
    assert out.status is VerificationStatus.UNVERIFIED
    assert out.confidence.level in (ConfidenceLevel.NONE, ConfidenceLevel.LOW)
    assert out.confidence.score <= 25


async def test_login_redirect_is_restricted(container: Container, web: MockWeb) -> None:
    web.add("https://t.me/privatething", httpx.Response(302, headers={"location": "https://t.me/login?next=x"}))
    web.add("https://t.me/login", httpx.Response(200, text="<html>login</html>"))
    out = await container.verification.verify(
        "https://t.me/privatething", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.RESTRICTED
    assert out.redirect_chain


async def test_redirect_off_platform_is_refused(container: Container, web: MockWeb) -> None:
    web.add("https://t.me/redirector", httpx.Response(302, headers={"location": "https://evil.example/x"}))
    out = await container.verification.verify(
        "https://t.me/redirector", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.RESTRICTED
    assert "evil.example" not in web.hosts_called()


async def test_server_error_is_inaccessible(container: Container, web: MockWeb) -> None:
    web.add("https://t.me/broken", httpx.Response(503))
    out = await container.verification.verify(
        "https://t.me/broken", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.INACCESSIBLE


async def test_robots_disallow_skips_live_check(container: Container, web: MockWeb) -> None:
    web.add("https://t.me/robots.txt", httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    out = await container.verification.verify(
        "https://t.me/acmehelp", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.UNVERIFIED
    assert ("t.me", "/acmehelp") not in [(r.url.host, r.url.path) for r in web.requests]
    assert any("robots.txt" in lim for lim in out.limitations)


async def test_telegram_preview_without_profile_is_not_found(container: Container, web: MockWeb) -> None:
    web.add("https://t.me/nosuchuser", httpx.Response(200, text="<html><title>Telegram</title></html>"))
    out = await container.verification.verify(
        "https://t.me/nosuchuser", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.NOT_FOUND


async def test_private_invite_links_are_never_opened(container: Container, web: MockWeb) -> None:
    out = await container.verification.verify(
        "https://t.me/+AbCdEfGhIjKl", variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED
    )
    assert out.status is VerificationStatus.UNVERIFIED
    assert web.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/profile",
        "http://127.0.0.1/",
        "https://t.me/share/url",
        "file:///etc/passwd",
        "https://user@t.me/acme",
    ],
)
async def test_invalid_or_unsupported_urls_are_errors(container: Container, web: MockWeb, url: str) -> None:
    out = await container.verification.verify(url, variants=VARIANTS, discovery_source=DiscoverySource.USER_PROVIDED)
    assert out.status is VerificationStatus.ERROR
    assert web.requests == []


async def test_whatsapp_generated_link_makes_no_claim(container: Container, web: MockWeb) -> None:
    from app.services.discovery.platforms import PLATFORMS, whatsapp_click_to_chat

    out = container.verification.generated_link(PLATFORMS["whatsapp"], whatsapp_click_to_chat("+919876543210"))
    assert out.status is VerificationStatus.UNVERIFIED
    assert out.confidence.score == 0
    assert "does not indicate" in out.evidence_description()
    assert web.requests == []
