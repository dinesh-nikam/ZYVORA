"""Investigation event bus (Redis pub/sub + bounded replay history)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis

_HISTORY_LIMIT = 200
_HISTORY_TTL = 86_400


class EventType(StrEnum):
    INVESTIGATION_CREATED = "investigation.created"
    INVESTIGATION_STARTED = "investigation.started"
    DISCOVERY_STARTED = "discovery.started"
    DISCOVERY_PROGRESS = "discovery.progress"
    PROFILE_DISCOVERED = "profile.discovered"
    PROFILE_VERIFIED = "profile.verified"
    INVESTIGATION_COMPLETED = "investigation.completed"
    INVESTIGATION_FAILED = "investigation.failed"
    INVESTIGATION_CANCELLED = "investigation.cancelled"


class EventBus:
    def __init__(self, redis: Redis, *, namespace: str = "pi:events") -> None:
        self._redis = redis
        self._ns = namespace

    def _channel(self, investigation_id: uuid.UUID) -> str:
        return f"{self._ns}:{investigation_id}"

    async def publish(
        self,
        investigation_id: uuid.UUID,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "type": event_type.value,
            "investigation_id": str(investigation_id),
            "timestamp": datetime.now(UTC).isoformat(),
            "data": data or {},
        }
        payload = json.dumps(event, default=str)
        channel = self._channel(investigation_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(f"{channel}:history", payload)
            pipe.ltrim(f"{channel}:history", -_HISTORY_LIMIT, -1)
            pipe.expire(f"{channel}:history", _HISTORY_TTL)
            pipe.publish(channel, payload)
            await pipe.execute()
        return event

    async def history(self, investigation_id: uuid.UUID) -> list[dict[str, Any]]:
        raw = await self._redis.lrange(f"{self._channel(investigation_id)}:history", 0, -1)  # type: ignore[misc]
        return [json.loads(item) for item in raw]

    async def subscribe(self, investigation_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel(investigation_id))
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    yield {"type": "heartbeat"}
                    continue
                if message.get("type") == "message":
                    yield json.loads(message["data"])
        finally:
            await pubsub.unsubscribe(self._channel(investigation_id))
            await pubsub.aclose()

    async def clear(self, investigation_id: uuid.UUID) -> None:
        await self._redis.delete(f"{self._channel(investigation_id)}:history")
