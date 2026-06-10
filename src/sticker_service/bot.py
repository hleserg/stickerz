"""Telegram bot entry point (long-polling runner).

Thin shell: the testable wiring lives in :mod:`sticker_service.handlers`.
Run with ``python -m sticker_service.bot`` (the Docker entrypoint) once
``BOT_TOKEN`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.fsm_storage import SqliteStorage
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

    if not settings.admin_id_list:
        logger.warning(
            "APP_ADMIN_IDS is empty — bug reports, error DMs and budget alerts "
            "will reach NO ONE; admin commands are unusable."
        )

    db = await Database.connect(settings.data_dir / "sticker_service.sqlite")
    loader = StyleLoader(settings.styles_dir)
    loader.load()
    model = build_model()
    orchestrator = Orchestrator(
        model=model,
        db=db,
        publisher=Publisher(bot, me.username or ""),
        loader=loader,
        storage_dir=settings.data_dir,
    )
    # Bound disk/DB growth: drop abandoned drafts + their PNGs once per boot.
    removed = await orchestrator.gc_stale_drafts(older_than_days=settings.draft_retention_days)
    if removed:
        logger.info("startup gc: removed %d stale draft pack(s)", removed)
    # Same for analytics events — except generation_done, which the alpha
    # budget counts all-time (services/budget.py).
    from sticker_service.services import analytics

    pruned = await db.prune_events(
        older_than_days=settings.events_retention_days,
        keep_events=(analytics.GENERATION_DONE,),
    )
    if pruned:
        logger.info("startup gc: pruned %d old analytics event(s)", pruned)

    # Persist FSM state so an OOM/restart resumes flows instead of dropping them.
    storage = await SqliteStorage.create(settings.data_dir / "fsm.sqlite")
    dp = build_dispatcher(db=db, orchestrator=orchestrator, loader=loader, storage=storage)
    await _set_commands(bot)

    # Keep the default-pack meme pool in step with Runet trends (weekly rewrite).
    from sticker_service.services.stickers.refresh_memes import meme_refresh_loop

    refresh_task = asyncio.create_task(
        meme_refresh_loop(model, db, days=settings.meme_refresh_days)
    )

    def _log_refresh_death(task: asyncio.Task[None]) -> None:
        # The loop is supposed to run forever; any exit besides our own
        # cancellation means the meme pool will silently go stale — say so.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("meme_refresh_loop died: %r — pool will go stale", exc)

    refresh_task.add_done_callback(_log_refresh_death)

    logger.info("Starting long-polling as @%s", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        await storage.close()
        await bot.session.close()
        await db.close()


async def _set_commands(bot: Bot) -> None:
    """Register the bot's command menu (the "/" list in Telegram clients)."""
    from aiogram.types import BotCommand

    await bot.set_my_commands(
        [
            BotCommand(command="new", description="Новый пак"),
            BotCommand(command="mychars", description="Мои персонажи (новый пак про того же)"),
            BotCommand(command="mypacks", description="Мои паки (открыть/опубликовать/скачать)"),
            BotCommand(command="addto", description="Дополнить существующий пак"),
            BotCommand(command="cancel", description="Отменить текущее действие"),
            BotCommand(command="help", description="Что умеет бот и цены"),
            BotCommand(command="rules", description="Правила"),
            BotCommand(command="report", description="Сообщить об ошибке"),
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
