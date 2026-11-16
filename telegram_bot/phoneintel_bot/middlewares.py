"""Access control applied before any handler runs:

1. private chats only (results must never be posted into groups/channels);
2. optional Telegram-user allowlist;
3. per-user rate limiting (in-memory, or Redis when configured).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Chat, Message, TelegramObject, User


class RateLimiter(Protocol):
    async def hit(self, user_id: int) -> float:
        """Register one event; return 0 if allowed, else seconds until the next allowed event."""
        ...


class MemoryRateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._events: dict[int, deque[float]] = defaultdict(deque)

    async def hit(self, user_id: int) -> float:
        now = time.monotonic()
        window = self._events[user_id]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return 60 - (now - window[0])
        window.append(now)
        return 0.0


class RedisRateLimiter:
    def __init__(self, redis: Any, per_minute: int) -> None:
        self._redis = redis
        self.per_minute = per_minute

    async def hit(self, user_id: int) -> float:
        bucket = int(time.time() // 60)
        key = f"pi:bot:rl:{user_id}:{bucket}"
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, 61)
        if count > self.per_minute:
            return float(60 - int(time.time()) % 60)
        return 0.0


class AccessMiddleware(BaseMiddleware):
    def __init__(self, limiter: RateLimiter, allowed_user_ids: list[int]) -> None:
        self.limiter = limiter
        self.allowed = frozenset(allowed_user_ids)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        chat: Chat | None = data.get("event_chat")
        if user is None or user.is_bot:
            return None
        if chat is not None and chat.type != "private":
            if isinstance(event, Message) and (event.text or "").startswith("/"):
                await event.answer("For privacy, PHONEINTEL only works in a private chat with the bot.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Private chat only.", show_alert=True)
            return None
        if self.allowed and user.id not in self.allowed:
            if isinstance(event, Message):
                await event.answer("You are not authorised to use this bot.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Not authorised.", show_alert=True)
            return None
        wait = await self.limiter.hit(user.id)
        if wait > 0:
            text = f"Too many requests. Please wait {int(wait) + 1} s."
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            return None
        return await handler(event, data)
