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
from sticker_service.db import Database
from sticker_service.handlers import build_dispatcher
from sticker_service.observability import init_sentry
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.models import build_model
from sticker_service.services.orchestrator import Orchestrator
from sticker_service.services.publish import Publisher

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
    """Wire the full app (db, styles, model, publisher) and long-poll."""
    init_sentry()
    settings = get_settings()
    bot = build_bot()
    me = await bot.get_me()

    db = await Database.connect(settings.data_dir / "sticker_service.sqlite")
    loader = StyleLoader(settings.styles_dir)
    loader.load()
    orchestrator = Orchestrator(
        model=build_model(),
        db=db,
        publisher=Publisher(bot, me.username or ""),
        loader=loader,
        storage_dir=settings.data_dir,
    )
    dp = build_dispatcher(db=db, orchestrator=orchestrator, loader=loader)
    await _set_commands(bot)

    logger.info("Starting long-polling as @%s", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await db.close()


async def _set_commands(bot: Bot) -> None:
    """Register the bot's command menu (the "/" list in Telegram clients)."""
    from aiogram.types import BotCommand

    await bot.set_my_commands(
        [
            BotCommand(command="new", description="Новый пак"),
            BotCommand(command="mychars", description="Мои персонажи (новый пак про того же)"),
            BotCommand(command="addto", description="Дополнить существующий пак"),
            BotCommand(command="start", description="О боте"),
        ]
    )


def main() -> int:
    """Console / Docker entry point."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
