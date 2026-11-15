"""Official platform API clients used for live profile verification.

Only documented, official APIs are used.  Each client returns public fields
of the *given* profile; none of them performs phone-number-to-account lookup.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.errors import OutboundBlocked
from app.integrations.http import FetchResult, SafeHttpClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PlatformLookup:
    source_name: str
    api_url: str  # public, secret-free description of the endpoint used
    exists: bool | None
    restricted: bool = False
    status_code: int | None = None
    canonical_url: str | None = None
    display_name: str | None = None
    public_text: list[str] = field(default_factory=list)
    error: str | None = None


class PlatformApiClient(Protocol):
    platform: str
    source_name: str

    async def lookup(self, handle: str, kind: str) -> PlatformLookup: ...


def _json(result: FetchResult) -> dict[str, Any]:
    import json  # noqa: PLC0415

    try:
        data = json.loads(result.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _clip(value: object, limit: int = 1000) -> str:
    return str(value or "")[:limit]


async def _safe_fetch(http: SafeHttpClient, url: str, domains: set[str], source: str, api_url: str,
                      **kwargs: Any) -> FetchResult | PlatformLookup:
    try:
        return await http.fetch(url, allowed_domains=domains, target_label=source, **kwargs)
    except OutboundBlocked as exc:
        return PlatformLookup(source_name=source, api_url=api_url, exists=None, error=f"blocked: {exc.code}")
    except httpx.HTTPError as exc:
        logger.warning("platform API request failed", extra={"source": source, "error_type": type(exc).__name__})
        return PlatformLookup(source_name=source, api_url=api_url, exists=None, error="network_error")


class GitHubClient:
    platform = "github"
    source_name = "github_rest_api"

    def __init__(self, http: SafeHttpClient, token: str | None = None) -> None:
        self._http = http
        self._token = token

    async def lookup(self, handle: str, kind: str) -> PlatformLookup:
        api_url = f"https://api.github.com/users/{handle}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        result = await _safe_fetch(self._http, api_url, {"api.github.com"}, self.source_name, api_url,
                                   headers=headers)
        if isinstance(result, PlatformLookup):
            return result
        if result.status_code == 404:
            return PlatformLookup(self.source_name, api_url, exists=False, status_code=404)
        if result.status_code in (403, 429):
            return PlatformLookup(self.source_name, api_url, exists=None, status_code=result.status_code,
                                  error="rate_limited")
        if result.status_code != 200:
            return PlatformLookup(self.source_name, api_url, exists=None, status_code=result.status_code,
                                  error=f"http_{result.status_code}")
        data = _json(result)
        return PlatformLookup(
            source_name=self.source_name,
            api_url=api_url,
            exists=True,
            status_code=200,
            canonical_url=str(data.get("html_url") or f"https://github.com/{handle}").lower(),
            display_name=_clip(data.get("name") or data.get("login"), 200),
            public_text=[_clip(data.get(k)) for k in ("bio", "blog", "company", "twitter_username") if data.get(k)],
        )


class YouTubeClient:
    platform = "youtube"
    source_name = "youtube_data_api_v3"

    def __init__(self, http: SafeHttpClient, api_key: str) -> None:
        self._http = http
        self._key = api_key

    async def lookup(self, handle: str, kind: str) -> PlatformLookup:
        api_url = "https://www.googleapis.com/youtube/v3/channels"
        params: dict[str, str] = {"part": "snippet", "key": self._key}
        if kind == "channel_id":
            params["id"] = handle
        elif kind == "channel":
            params["forHandle"] = "@" + handle
        else:
            params["forUsername"] = handle
        result = await _safe_fetch(self._http, api_url, {"www.googleapis.com"}, self.source_name, api_url,
                                   params=params)
        if isinstance(result, PlatformLookup):
            return result
        if result.status_code in (403, 429):
            return PlatformLookup(self.source_name, api_url, exists=None, status_code=result.status_code,
                                  error="quota_or_forbidden")
        if result.status_code != 200:
            return PlatformLookup(self.source_name, api_url, exists=None, status_code=result.status_code,
                                  error=f"http_{result.status_code}")
        items = _json(result).get("items") or []
        if not items:
            return PlatformLookup(self.source_name, api_url, exists=False, status_code=200)
        item = items[0]
        snippet = item.get("snippet") or {}
        custom = snippet.get("customUrl")
        canonical = (f"https://www.youtube.com/{custom}".lower() if custom
                     else f"https://www.youtube.com/channel/{item.get('id')}")
        return PlatformLookup(
            source_name=self.source_name, api_url=api_url, exists=True, status_code=200,
            canonical_url=canonical, display_name=_clip(snippet.get("title"), 200),
            public_text=[_clip(snippet.get("description"), 5000)],
        )


class RedditClient:
    platform = "reddit"
    source_name = "reddit_oauth_api"

    def __init__(self, http: SafeHttpClient, client_id: str, client_secret: str, user_agent: str) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._ua = user_agent
        self._token: str | None = None
        self._token_expiry = 0.0

    async def _access_token(self) -> str | None:
        if self._token and time.monotonic() < self._token_expiry - 30:
            return self._token
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        try:
            result = await self._http.fetch(
                "https://www.reddit.com/api/v1/access_token", allowed_domains={"www.reddit.com"}, method="POST",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}", "User-Agent": self._ua}, target_label=self.source_name,
            )
        except (OutboundBlocked, httpx.HTTPError):
            logger.warning("reddit token request failed")
            return None
        if result.status_code != 200:
            logger.warning("reddit token request rejected", extra={"status": result.status_code})
            return None
        data = _json(result)
        self._token = str(data.get("access_token") or "") or None
        self._token_expiry = time.monotonic() + float(data.get("expires_in") or 0)
        return self._token

    async def lookup(self, handle: str, kind: str) -> PlatformLookup:
        api_url = f"https://oauth.reddit.com/user/{handle}/about"
        token = await self._access_token()
        if token is None:
            return PlatformLookup(self.source_name, api_url, exists=None, error="auth_failed")
        result = await _safe_fetch(self._http, api_url, {"oauth.reddit.com"}, self.source_name, api_url,
                                   headers={"Authorization": f"Bearer {token}", "User-Agent": self._ua})
        if isinstance(result, PlatformLookup):
            return result
        if result.status_code == 404:
            return PlatformLookup(self.source_name, api_url, exists=False, status_code=404)
        if result.status_code in (401, 403):
            return PlatformLookup(self.source_name, api_url, exists=None, restricted=True,
                                  status_code=result.status_code)
        if result.status_code != 200:
            return PlatformLookup(self.source_name, api_url, exists=None, status_code=result.status_code,
                                  error=f"http_{result.status_code}")
        data = _json(result).get("data") or {}
        if data.get("is_suspended"):
            return PlatformLookup(self.source_name, api_url, exists=True, restricted=True, status_code=200)
        sub = data.get("subreddit") or {}
        return PlatformLookup(
            source_name=self.source_name, api_url=api_url, exists=True, status_code=200,
            canonical_url=f"https://www.reddit.com/user/{handle}/",
            display_name=_clip(data.get("name"), 100),
            public_text=[_clip(sub.get("public_description"), 2000), _clip(sub.get("title"), 300)],
        )


class TelegramBotApiClient:
    """Uses the official Bot API ``getChat`` for *public* channels and groups."""

    platform = "telegram"
    source_name = "telegram_bot_api"

    def __init__(self, http: SafeHttpClient, bot_token: str) -> None:
        self._http = http
        self._token = bot_token

    async def lookup(self, handle: str, kind: str) -> PlatformLookup:
        public_api_url = "https://api.telegram.org/bot<token>/getChat"
        url = f"https://api.telegram.org/bot{self._token}/getChat"
        result = await _safe_fetch(self._http, url, {"api.telegram.org"}, self.source_name, public_api_url,
                                   params={"chat_id": "@" + handle})
        if isinstance(result, PlatformLookup):
            return result
        data = _json(result)
        if result.status_code == 200 and data.get("ok"):
            chat = data.get("result") or {}
            return PlatformLookup(
                source_name=self.source_name, api_url=public_api_url, exists=True, status_code=200,
                canonical_url=f"https://t.me/{str(chat.get('username') or handle).lower()}",
                display_name=_clip(chat.get("title") or chat.get("first_name"), 200),
                public_text=[_clip(chat.get("description") or chat.get("bio"), 2000)],
            )
        # Bot API cannot resolve most *user* accounts by username; this is not proof of absence.
        return PlatformLookup(self.source_name, public_api_url, exists=None, status_code=result.status_code,
                              error="not_resolvable_via_bot_api")
