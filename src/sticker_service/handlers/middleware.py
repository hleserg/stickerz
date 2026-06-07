"""Whitelist access control as an outer middleware (§11.1, §B.4).

Runs before any handler: a non-whitelisted user gets a polite refusal and the
update is dropped. Admins (from config) are always allowed and auto-added to the
whitelist on first contact. Identity key is the durable Telegram ``user_id``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component

DENIAL = "Доступ ограничен: бот в закрытом тестировании."


class WhitelistMiddleware(BaseMiddleware):
    """Block updates from users who are not on the whitelist."""

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

        if user.id in get_settings().admin_id_set:
            await self._db.allow(user.id, getattr(user, "username", None))
            return await handler(event, data)

        if await self._db.is_allowed(user.id):
            return await handler(event, data)

        answer = getattr(event, "answer", None)
        if answer is not None:
            await answer(DENIAL)
        return None
