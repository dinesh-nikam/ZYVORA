from __future__ import annotations

import httpx
import pytest
from app.integrations.http import SafeHttpClient
from app.integrations.search.providers import (
    BraveSearchProvider,
    GoogleCseProvider,
    SearchProviderError,
    SearxngProvider,
    build_search_providers,
)
from app.services.discovery.service import DiscoveryService, normalize_result_url
from app.services.phone.matching import build_variants

from tests.conftest import MockWeb, default_web, make_settings


def client(tmp_path, web: MockWeb, **kw) -> SafeHttpClient:  # type: ignore[no-untyped-def]
    return SafeHttpClient(make_settings(tmp_path, **kw), resolver=web.resolve, transport=web.transport())


async def test_brave_parsing_and_auth_header(tmp_path) -> None:
    web = default_web()
    http = client(tmp_path, web)
    hits = await BraveSearchProvider(http, "k-123").search('"+919876543210"', count=10)
    assert hits[0].url == "https://www.instagram.com/acme_support/"
    assert "<strong>" not in hits[1].snippet
    request = web.requests[0]
    assert request.headers["X-Subscription-Token"] == "k-123"
    assert request.url.params["q"] == '"+919876543210"'
    await http.aclose()


async def test_google_cse_parsing(tmp_path) -> None:
    web = MockWeb()
    web.add(
        "https://www.googleapis.com/customsearch/v1",
        httpx.Response(
            200, json={"items": [{"link": "https://t.me/acmehelp", "title": "ACME", "snippet": "Call +91 98765 43210"}]}
        ),
    )
    http = client(tmp_path, web)
    hits = await GoogleCseProvider(http, "key", "cx1").search("q", count=5)
    assert hits[0].url == "https://t.me/acmehelp"
    assert web.requests[0].url.params["cx"] == "cx1"
    await http.aclose()


async def test_searxng_trusted_endpoint(tmp_path) -> None:
    web = MockWeb()
    web.add(
        "http://search.example.net:8080/search",
        httpx.Response(
            200, json={"results": [{"url": "https://github.com/acmedev", "title": "acmedev", "content": "hotline"}]}
        ),
    )
    http = client(tmp_path, web)
    hits = await SearxngProvider(http, "http://search.example.net:8080").search("q", count=5)
    assert hits[0].url == "https://github.com/acmedev"
    await http.aclose()


@pytest.mark.parametrize(("status", "code"), [(429, "rate_limited"), (401, "auth_failed"), (500, "http_500")])
async def test_provider_errors_are_typed(tmp_path, status: int, code: str) -> None:
    web = MockWeb()
    web.add("https://api.search.brave.com/res/v1/web/search", httpx.Response(status, json={}))
    http = client(tmp_path, web)
    with pytest.raises(SearchProviderError) as exc:
        await BraveSearchProvider(http, "k").search("q", count=5)
    assert exc.value.code == code
    await http.aclose()


async def test_invalid_json_is_reported(tmp_path) -> None:
    web = MockWeb()
    web.add("https://api.search.brave.com/res/v1/web/search", httpx.Response(200, text="<html>"))
    http = client(tmp_path, web)
    with pytest.raises(SearchProviderError) as exc:
        await BraveSearchProvider(http, "k").search("q", count=5)
    assert exc.value.code == "bad_response"
    await http.aclose()


def test_only_credentialed_providers_are_built(tmp_path) -> None:
    web = MockWeb()
    settings = make_settings(
        tmp_path,
        brave_api_key=None,
        search_providers="brave,google_cse,searxng",
        google_cse_api_key="g",
        google_cse_cx=None,
        searxng_base_url=None,
    )
    assert build_search_providers(settings, client(tmp_path, web)) == []


async def test_discovery_dedupes_and_extracts(tmp_path) -> None:
    web = default_web()
    http = client(tmp_path, web)
    service = DiscoveryService([BraveSearchProvider(http, "k")], max_queries=3)
    run = await service.search(build_variants("+919876543210"), ["telegram", "instagram", "facebook", "github"])
    urls = set(run.candidates)
    assert "https://www.instagram.com/acme_support/" in urls  # profile URL returned by the index
    assert "https://t.me/acmehelp" in urls  # extracted from a third-party snippet
    assert "https://github.com/acmedev" in urls
    assert "https://www.facebook.com/john.doe.123" in urls  # returned, but carries no number evidence
    fb = run.candidates["https://www.facebook.com/john.doe.123"]
    assert not any(e.phone_reference_found for e in fb.evidence)
    tg = run.candidates["https://t.me/acmehelp"]
    assert all(not e.on_profile for e in tg.evidence)
    assert run.web_mentions_without_profile >= 1
    assert run.queries_executed == 3
    assert run.unique_results < run.results_seen  # duplicates removed
    await http.aclose()


def test_query_builder_scopes_sites() -> None:
    service = DiscoveryService([], max_queries=5)
    queries = service.build_queries(build_variants("+919876543210"), ["telegram", "linkedin"])
    assert queries[0].startswith('"+919876543210"')
    assert "site:t.me" in queries[1]
    assert "site:linkedin.com/in" in queries[2]


def test_result_url_normalisation() -> None:
    assert normalize_result_url("https://www.instagram.com/acme/") == normalize_result_url("https://instagram.com/acme")
