"""Global error handler: log unhandled exceptions and DM the first admin.

The first admin (owner) receives when / who / where for every unhandled error,
so issues surface immediately during testing.
"""

from __future__ import annotations

import contextlib
import logging
import traceback
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.types import ErrorEvent

from sticker_service.config import get_settings
from sticker_service.observability import tag_component

logger = logging.getLogger(__name__)


def _extract_user(update: Any) -> Any:
    for attr in ("message", "callback_query", "edited_message"):
        obj = getattr(update, attr, None)
        if obj is not None and getattr(obj, "from_user", None) is not None:
            return obj.from_user
    return None


def _user_ref(user: Any) -> str:
    if user is None:
        return "неизвестно"
    handle = f"@{user.username}" if getattr(user, "username", None) else "—"
    return f"id={user.id} {handle} (tg://user?id={user.id})"


async def on_error(event: ErrorEvent, bot: Bot) -> bool:
    """Log + forward the unhandled error to the first admin. Returns handled=True."""
    tag_component("handlers.errors")
    exc = event.exception
    logger.exception("unhandled error", exc_info=exc)
    admin = get_settings().first_admin_id
    if admin is None:
        return True
    who = _user_ref(_extract_user(event.update))
    summary = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-1500:]
    text = (
        "⚠️ Необработанная ошибка\n"
        f"Когда: {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC\n"
        f"У кого: {who}\n"
        f"Где: {summary}\n\n"
        f"<pre>{tb}</pre>"
    )
    with contextlib.suppress(Exception):
        await bot.send_message(admin, text, parse_mode="HTML")
    return True
