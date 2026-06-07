"""Telegram bot entry point (long-polling runner).

Thin shell: the testable wiring lives in :mod:`sticker_service.handlers`.
Run with ``python -m sticker_service.bot`` (the Docker entrypoint) once
``BOT_TOKEN`` is set.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

from sticker_service.config import get_settings
from sticker_service.handlers import build_dispatcher
from sticker_service.observability import init_sentry

logger = logging.getLogger(__name__)


def build_bot(token: str | None = None) -> Bot:
    """Create the :class:`Bot`, resolving the token from settings if not given.

    Raises ``RuntimeError`` with a clear message when no token is available,
    rather than failing obscurely deep inside aiogram.
    """
    resolved = token if token is not None else get_settings().bot_token
    if not resolved:
        raise RuntimeError("BOT_TOKEN is not set — cannot start the bot.")
    return Bot(resolved)


async def run() -> None:
    """Initialize observability and long-poll until cancelled."""
    init_sentry()
    bot = build_bot()
    dp = build_dispatcher()
    logger.info("Starting long-polling")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


def main() -> int:
    """Console / Docker entry point."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
