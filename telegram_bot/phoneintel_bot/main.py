"""Main entry point for the PHONEINTEL Telegram Bot."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from phoneintel_bot.client import BackendClient
from phoneintel_bot.config import BotSettings
from phoneintel_bot.handlers import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("phoneintel_bot")


def build_bot(settings: BotSettings) -> tuple[Bot, Dispatcher]:
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher()
    dp.include_router(router)
    backend = BackendClient(settings)
    dp["backend"] = backend
    return bot, dp


async def main() -> None:
    settings = BotSettings()
    token = settings.bot_token.get_secret_value()
    if not token or "PLACEHOLDER" in token:
        logger.error("TELEGRAM_BOT_TOKEN is not configured. Please set TELEGRAM_BOT_TOKEN in .env")
        sys.exit(1)

    bot, dp = build_bot(settings)
    logger.info("Starting PHONEINTEL Telegram Bot...")
    await dp.start_polling(bot)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
