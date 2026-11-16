"""Redis-backed fixed-window rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.errors import RateLimited

_PERIODS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    limit: int
    window_seconds: int

    @classmethod
    def parse(cls, spec: str) -> RateLimitRule:
        try:
            count, period = spec.strip().split("/", 1)
            limit = int(count)
            window = _PERIODS[period.strip().lower()]
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid rate limit spec {spec!r}; expected '<n>/<second|minute|hour|day>'") from exc
        if limit <= 0:
            raise ValueError("rate limit must be positive")
        return cls(limit=limit, window_seconds=window)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter:
    def __init__(self, redis: Redis, *, namespace: str = "pi:rl") -> None:
        self._redis = redis
        self._ns = namespace

    async def hit(self, scope: str, identity: str, rule: RateLimitRule) -> RateLimitResult:
        now = int(time.time())
        window_start = now - (now % rule.window_seconds)
        key = f"{self._ns}:{scope}:{identity}:{window_start}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, rule.window_seconds + 1)
            count, _ = await pipe.execute()
        count = int(count)
        retry_after = window_start + rule.window_seconds - now
        return RateLimitResult(
            allowed=count <= rule.limit, remaining=max(rule.limit - count, 0), retry_after=max(retry_after, 1)
        )

    async def enforce(self, scope: str, identity: str, rule: RateLimitRule) -> RateLimitResult:
        result = await self.hit(scope, identity, rule)
        if not result.allowed:
            raise RateLimited(
                "Rate limit exceeded, retry later",
                headers={"Retry-After": str(result.retry_after)},
                details=[{"scope": scope, "retry_after_seconds": result.retry_after}],
            )
        return result
