"""Bot entrypoint: ``phoneintel-bot`` (long polling)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from phoneintel_bot.client import BackendClient
from phoneintel_bot.config import BotSettings
from phoneintel_bot.handlers import ProgressTracker, router
from phoneintel_bot.middlewares import AccessMiddleware, MemoryRateLimiter, RateLimiter, RedisRateLimiter

logger = logging.getLogger("phoneintel_bot")

COMMANDS = [
    BotCommand(command="validate", description="Validate a phone number"),
    BotCommand(command="country", description="Country of a number"),
    BotCommand(command="carrier", description="Carrier and number type"),
    BotCommand(command="profile_links", description="Discover public profile links"),
    BotCommand(command="search_public", description="Public web index search"),
    BotCommand(command="report", description="Download a report"),
    BotCommand(command="history", description="Your investigations"),
    BotCommand(command="cancel", description="Cancel the current action"),
    BotCommand(command="delete_data", description="Erase your data"),
    BotCommand(command="privacy", description="Privacy notice"),
    BotCommand(command="help", description="Help"),
]


def build_dispatcher(
    settings: BotSettings,
    backend: BackendClient,
    *,
    storage: BaseStorage | None = None,
    limiter: RateLimiter | None = None,
) -> tuple[Dispatcher, ProgressTracker]:
    if storage is None:
        if settings.redis_url:
            from aiogram.fsm.storage.redis import RedisStorage  # noqa: PLC0415

            # Short TTLs: FSM data briefly holds the number being investigated.
            storage = RedisStorage.from_url(settings.redis_url, state_ttl=900, data_ttl=900)
        else:
            storage = MemoryStorage()
    if limiter is None:
        if settings.redis_url:
            from redis.asyncio import Redis  # noqa: PLC0415

            limiter = RedisRateLimiter(Redis.from_url(settings.redis_url), settings.rate_limit_per_minute)
        else:
            limiter = MemoryRateLimiter(settings.rate_limit_per_minute)
    tracker = ProgressTracker(backend, settings)
    dp = Dispatcher(storage=storage)
    access = AccessMiddleware(limiter, settings.allowed_user_ids)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    router._parent_router = None
    dp.include_router(router)
    dp["backend"] = backend
    dp["tracker"] = tracker
    dp["settings"] = settings
    return dp, tracker


def build_bot(settings: BotSettings) -> Bot:
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


async def main() -> None:
    settings = BotSettings()
    if not settings.bot_token.get_secret_value():
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be configured in environment or .env")
    if not settings.service_api_key.get_secret_value():
        raise RuntimeError("TELEGRAM_SERVICE_API_KEY must be configured in environment or .env")
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # never log request URLs/headers
    backend = BackendClient(settings)
    bot = build_bot(settings)
    dp, tracker = build_dispatcher(settings, backend)
    try:
        await bot.set_my_commands(COMMANDS)
        logger.info("PHONEINTEL bot starting (long polling)")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await tracker.shutdown()
        await backend.aclose()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
