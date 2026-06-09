"""Mode-aware access control as an outer middleware (§11.1, §B.4).

Runs before any handler and gates purely on the bot's operating mode:
- admins always pass (and are auto-whitelisted) — they bypass mode/ban gates;
- in ``debug`` only admins pass; everyone else gets a soft "under construction";
- in ``alpha`` everyone passes the middleware (pack creation is gated to approved
  participants inside the handlers), except users under an auto-moderation ban.

Identity key is the durable Telegram ``user_id``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import isolated_scope, tag_component
from sticker_service.services import modes


class SentryScopeMiddleware(BaseMiddleware):
    """Run every update in its own Sentry scope so handler tags don't leak.

    Registered as the outermost middleware: ``tag_component`` calls anywhere in
    the update's handling write to this per-update scope and are discarded when
    it returns, instead of accumulating on the process-wide current scope.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        with isolated_scope():
            return await handler(event, data)


class WhitelistMiddleware(BaseMiddleware):
    """Gate updates by operating mode (debug → admins only; alpha → all but banned)."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tag_component("handlers.middleware")
        user = data.get("event_from_user")
        if user is None:  # service updates without a user — let aiogram handle
            return await handler(event, data)

        answer = getattr(event, "answer", None)
        is_admin = user.id in get_settings().admin_id_set
        mode = await self._db.get_config("mode", modes.DEFAULT)

        # Admins always pass (and are auto-whitelisted), bypassing mode/ban gates.
        if is_admin:
            await self._db.allow(user.id, getattr(user, "username", None))
            return await handler(event, data)

        # Debug: only admins; everyone else gets a soft notice.
        if mode == modes.DEBUG:
            if answer is not None:
                await answer(
                    "🛠 Бот сейчас в разработке — скоро мы всё покажем! Загляни чуть позже."
                )
            return None

        # Temporary ban (auto-moderation) applies in any non-debug mode.
        until = await self._db.banned_until(user.id)
        if until is not None:
            if answer is not None:
                await answer(
                    "🚫 Вы временно заблокированы за нарушение правил (/rules) до "
                    f"{until.astimezone():%d.%m %H:%M}."
                )
            return None

        # Alpha (the only other implemented mode): everyone passes the middleware;
        # pack creation is gated to approved participants in the handlers.
        return await handler(event, data)
