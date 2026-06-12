"""Telegram bot entry point (long-polling runner).

Thin shell: the testable wiring lives in :mod:`sticker_service.handlers`.
Run with ``python -m sticker_service.bot`` (the Docker entrypoint) once
``BOT_TOKEN`` is set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, FSInputFile

from sticker_service.config import Settings, get_settings
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

    async def _owner_evidence(text: str, attachment: Path | None) -> None:
        """Scene-observer channel: the sheet evidence goes straight to the owner."""
        owner = settings.first_admin_id
        if owner is None:
            return
        with contextlib.suppress(Exception):
            if attachment is not None:
                await bot.send_document(owner, FSInputFile(attachment), caption=text[:1000])
            else:
                await bot.send_message(owner, text[:4000])

    orchestrator = Orchestrator(
        model=model,
        db=db,
        publisher=Publisher(bot, me.username or ""),
        loader=loader,
        storage_dir=settings.data_dir,
        owner_notify=_owner_evidence,
    )
    # Persist FSM state so an OOM/restart resumes flows instead of dropping them.
    storage = await SqliteStorage.create(settings.data_dir / "fsm.sqlite")
    dp = build_dispatcher(db=db, orchestrator=orchestrator, loader=loader, storage=storage)
    await _set_commands(bot, settings)

    # Bound disk/DB growth while the container lives, not just at boot: GC stale
    # drafts, prune analytics, sweep abandoned FSM rows, warn before disk fills.
    from sticker_service.maintenance.loop import maintenance_loop

    async def _notify_admins(text: str) -> None:
        for admin_id in settings.admin_id_list:
            with contextlib.suppress(Exception):
                await bot.send_message(admin_id, text)

    maintenance_task = asyncio.create_task(
        maintenance_loop(orchestrator=orchestrator, db=db, storage=storage, notify=_notify_admins)
    )

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

    # A hard restart (crash/OOM/SIGKILL) strands users mid-generation with a
    # frozen status message; give every such user a resume button right away.
    from sticker_service.handlers import flow

    with contextlib.suppress(Exception):
        await flow.revive_orphaned_generations(bot, storage)

    logger.info("Starting long-polling as @%s", me.username)
    try:
        # Bound concurrent update-handler tasks: without a cap, update bursts
        # (or button spam) fan out into unbounded coroutines and memory.
        await dp.start_polling(bot, tasks_concurrency_limit=settings.polling_tasks_limit or None)
    finally:
        for task in (refresh_task, maintenance_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # Polling has stopped, but generations run as detached tasks aiogram
        # does not wait for. Drain them BEFORE closing sessions (they still
        # edit chat messages / write the DB); 140s fits compose's 150s grace.
        await flow.drain_generations(timeout=140.0)
        await storage.close()
        await bot.session.close()
        await db.close()


def _user_commands() -> list[BotCommand]:
    """The "/" menu every user sees."""
    return [
        BotCommand(command="new", description="Новый пак"),
        BotCommand(command="mychars", description="Мои персонажи (новый пак про того же)"),
        BotCommand(command="mypacks", description="Мои паки (открыть/опубликовать/скачать)"),
        BotCommand(command="history", description="История заказов: какие стикеры я просил"),
        BotCommand(command="addto", description="Дополнить существующий пак"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
        BotCommand(command="help", description="Что умеет бот и цены"),
        BotCommand(command="rules", description="Правила"),
        BotCommand(command="report", description="Сообщить об ошибке"),
        BotCommand(command="start", description="О боте"),
    ]


def _admin_commands() -> list[BotCommand]:
    """Extra menu entries every admin gets (their toolbox sits on top)."""
    return [
        BotCommand(command="users", description="👥 Пользователи (кнопки: доступ, паки, чат)"),
        BotCommand(command="stats", description="📊 Статистика и воронка"),
        BotCommand(command="waiting", description="⏳ Заявки на рассмотрении"),
        BotCommand(command="approved", description="✅ Одобренные заявки"),
        BotCommand(command="rejected", description="🚫 Отклонённые заявки"),
        BotCommand(command="bans", description="🔨 Активные баны (снять)"),
    ]


def _owner_commands() -> list[BotCommand]:
    """Extra menu entries only the FIRST admin sees (mode/budget switches)."""
    return [
        BotCommand(command="mode", description="Режим бота: отладка/альфа"),
        BotCommand(command="setbudget", description="Бюджет альфы: /setbudget <$>"),
    ]


def commands_for(*, admin: bool, owner: bool) -> list[BotCommand]:
    """The full scoped menu for one chat: toolbox first, daily commands after."""
    commands: list[BotCommand] = []
    if admin:
        commands += _admin_commands()
    if owner:
        commands += _owner_commands()
    return commands + _user_commands()


async def _set_commands(bot: Bot, settings: Settings) -> None:
    """Register scoped "/" menus: users, admins, and the first admin (owner).

    The default scope carries only user commands; each admin's private chat
    additionally gets the admin toolbox, and the owner also gets mode/budget.
    A per-chat scope fails with 400 until that user has talked to the bot —
    logged and skipped; the menu appears on the restart after first contact.
    """
    await bot.set_my_commands(_user_commands())
    # Groups/channels: only /start makes sense (the rest is a private workspace).
    with contextlib.suppress(Exception):
        await bot.set_my_commands(
            [BotCommand(command="start", description="О боте")],
            scope=BotCommandScopeAllGroupChats(),
        )
    first_admin = settings.first_admin_id
    for admin_id in settings.admin_id_list:
        scoped = commands_for(admin=True, owner=admin_id == first_admin)
        try:
            await bot.set_my_commands(scoped, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:
            logger.warning("could not set admin menu for %s: %s", admin_id, str(exc)[:100])


def main() -> int:
    """Console / Docker entry point."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
