"""Redis client factory.

``memory://`` creates an in-process fakeredis instance.  It exists only so
developers and the test-suite can run the full stack without a Redis server;
``Settings`` refuses it in staging/production.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis


def create_redis(url: str) -> Redis:
    if url.startswith("memory://"):
        try:
            from fakeredis import FakeAsyncRedis  # noqa: PLC0415 - dev/test-only dependency
        except ImportError as exc:  # pragma: no cover - depends on install profile
            raise RuntimeError("REDIS_URL=memory:// requires the 'fakeredis' dev dependency") from exc
        client: Any = FakeAsyncRedis(decode_responses=True)
        return client  # type: ignore[no-any-return]
    return Redis.from_url(url, decode_responses=True, health_check_interval=30, socket_timeout=5)
