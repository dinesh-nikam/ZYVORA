"""robots.txt compliance (RFC 9309) for live public-page checks."""

from __future__ import annotations

import logging
import time
from urllib.robotparser import RobotFileParser

import httpx

from app.core.errors import OutboundBlocked
from app.integrations.http import SafeHttpClient

logger = logging.getLogger(__name__)

_CACHE_TTL = 3600.0


class RobotsPolicy:
    def __init__(self, http: SafeHttpClient, user_agent_name: str = "PhoneIntelBot") -> None:
        self._http = http
        self._ua = user_agent_name
        self._cache: dict[str, tuple[float, RobotFileParser | None, bool]] = {}

    async def allowed(self, url: str, *, allowed_domains: frozenset[str]) -> bool:
        parsed = httpx.URL(url)
        origin = f"{parsed.scheme}://{parsed.host}"
        cached = self._cache.get(origin)
        if cached is None or time.monotonic() - cached[0] > _CACHE_TTL:
            cached = (time.monotonic(), *await self._load(origin, allowed_domains))
            self._cache[origin] = cached
        _, parser, allow_all = cached
        if parser is None:
            return allow_all
        return parser.can_fetch(self._ua, url)

    async def _load(self, origin: str, allowed_domains: frozenset[str]) -> tuple[RobotFileParser | None, bool]:
        try:
            result = await self._http.fetch(f"{origin}/robots.txt", allowed_domains=allowed_domains,
                                            max_bytes=512_000, target_label="robots")
        except (OutboundBlocked, httpx.HTTPError):
            # RFC 9309 §2.3.1.4: unreachable robots.txt => assume complete disallow.
            logger.info("robots.txt unreachable; treating origin as disallowed", extra={"origin": origin})
            return None, False
        if 400 <= result.status_code < 500:
            return None, True  # "unavailable" => crawler may access any resource
        if result.status_code >= 500 or result.status_code != 200:
            return None, False
        parser = RobotFileParser()
        parser.parse(result.text().splitlines())
        return parser, True
