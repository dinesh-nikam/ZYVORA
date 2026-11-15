"""Pluggable, credentialed public web-search providers.

Only official search APIs are used (no SERP scraping, no CAPTCHA bypass).
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.errors import OutboundBlocked
from app.core.metrics import SEARCH_PROVIDER_ERRORS
from app.integrations.http import FetchResult, SafeHttpClient

logger = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]{0,200}>")


def clean_text(value: object, limit: int = 600) -> str:
    text = html.unescape(_TAG.sub("", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


@dataclass(frozen=True, slots=True)
class SearchHit:
    provider: str
    query: str
    rank: int
    url: str
    title: str
    snippet: str
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class SearchProviderError(Exception):
    def __init__(self, provider: str, code: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, count: int) -> list[SearchHit]: ...


def _decode(provider: str, result: FetchResult) -> dict[str, Any]:
    if result.status_code == 429:
        raise SearchProviderError(provider, "rate_limited", "provider rate limit reached")
    if result.status_code in (401, 403):
        raise SearchProviderError(provider, "auth_failed", "provider rejected credentials")
    if result.status_code != 200:
        raise SearchProviderError(provider, f"http_{result.status_code}", "provider returned an error")
    try:
        data = json.loads(result.body)
    except ValueError as exc:
        raise SearchProviderError(provider, "bad_response", "provider returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise SearchProviderError(provider, "bad_response", "unexpected provider payload")
    return data


async def _get(http: SafeHttpClient, provider: str, url: str, domains: set[str] | None, *,
               trusted: bool = False, **kwargs: Any) -> FetchResult:
    try:
        return await http.fetch(url, allowed_domains=domains, target_label=f"search:{provider}",
                                trusted=trusted, allow_http=trusted, max_bytes=2_000_000, **kwargs)
    except OutboundBlocked as exc:
        SEARCH_PROVIDER_ERRORS.labels(provider=provider, code="blocked").inc()
        raise SearchProviderError(provider, "blocked", exc.message) from exc
    except httpx.HTTPError as exc:
        SEARCH_PROVIDER_ERRORS.labels(provider=provider, code="network_error").inc()
        raise SearchProviderError(provider, "network_error", type(exc).__name__) from exc


class BraveSearchProvider:
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, http: SafeHttpClient, api_key: str) -> None:
        self._http = http
        self._key = api_key

    async def search(self, query: str, *, count: int) -> list[SearchHit]:
        result = await _get(self._http, self.name, self.endpoint, {"api.search.brave.com"},
                            params={"q": query, "count": min(count, 20), "safesearch": "moderate"},
                            headers={"Accept": "application/json", "X-Subscription-Token": self._key})
        data = _decode(self.name, result)
        items = (data.get("web") or {}).get("results") or []
        return [
            SearchHit(self.name, query, i + 1, str(item.get("url", "")), clean_text(item.get("title"), 300),
                      clean_text(" ".join([str(item.get("description") or "")]
                                          + [str(s) for s in item.get("extra_snippets") or []])))
            for i, item in enumerate(items) if item.get("url")
        ]


class GoogleCseProvider:
    name = "google_cse"
    endpoint = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, http: SafeHttpClient, api_key: str, cx: str) -> None:
        self._http = http
        self._key = api_key
        self._cx = cx

    async def search(self, query: str, *, count: int) -> list[SearchHit]:
        result = await _get(self._http, self.name, self.endpoint, {"www.googleapis.com"},
                            params={"key": self._key, "cx": self._cx, "q": query, "num": min(count, 10),
                                    "safe": "active"})
        data = _decode(self.name, result)
        return [
            SearchHit(self.name, query, i + 1, str(item.get("link", "")), clean_text(item.get("title"), 300),
                      clean_text(item.get("snippet")))
            for i, item in enumerate(data.get("items") or []) if item.get("link")
        ]


class SearxngProvider:
    """Self-hosted SearXNG instance (operator-configured, therefore trusted)."""

    name = "searxng"

    def __init__(self, http: SafeHttpClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def search(self, query: str, *, count: int) -> list[SearchHit]:
        result = await _get(self._http, self.name, f"{self._base}/search", None, trusted=True,
                            params={"q": query, "format": "json", "safesearch": 1},
                            headers={"Accept": "application/json"})
        data = _decode(self.name, result)
        return [
            SearchHit(self.name, query, i + 1, str(item.get("url", "")), clean_text(item.get("title"), 300),
                      clean_text(item.get("content")))
            for i, item in enumerate((data.get("results") or [])[:count]) if item.get("url")
        ]


def build_search_providers(settings: Settings, http: SafeHttpClient,
                           searxng_http: SafeHttpClient | None = None) -> list[SearchProvider]:
    providers: list[SearchProvider] = []
    for name in settings.configured_search_providers():
        if name == "brave" and settings.brave_api_key:
            providers.append(BraveSearchProvider(http, settings.brave_api_key.get_secret_value()))
        elif name == "google_cse" and settings.google_cse_api_key and settings.google_cse_cx:
            providers.append(GoogleCseProvider(http, settings.google_cse_api_key.get_secret_value(),
                                               settings.google_cse_cx))
        elif name == "searxng" and settings.searxng_base_url:
            providers.append(SearxngProvider(searxng_http or http, settings.searxng_base_url))
    return providers
