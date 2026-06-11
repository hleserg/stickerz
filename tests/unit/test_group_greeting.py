"""Tests for the group/channel greeting + DM invite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest_asyncio

from sticker_service.db import Database
from sticker_service.handlers.start import (
    GROUP_WELCOME,
    _bot_addressed,
    build_router,
    greet_group,
)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def _bot(username: str = "yuki_stickers_bot", bot_id: int = 999) -> AsyncMock:
    bot = AsyncMock()
    bot.me.return_value = SimpleNamespace(id=bot_id, username=username, is_bot=True)
    return bot


_DEFAULT_USER = SimpleNamespace(id=1, is_bot=False)


def _msg(
    text: str = "",
    *,
    reply_to: Any = None,
    from_user: Any = _DEFAULT_USER,
    bot: Any = None,
) -> AsyncMock:
    m = AsyncMock()
    m.text = text
    m.caption = None
    m.reply_to_message = reply_to
    m.from_user = from_user
    m.bot = bot or _bot()
    return m


async def test_greet_group_posts_welcome_with_dm_button(db: Database) -> None:
    msg = _msg()
    await greet_group(msg, db)
    args, kwargs = msg.answer.await_args
    assert args[0] == GROUP_WELCOME
    buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any(b.url == "https://t.me/yuki_stickers_bot" for b in buttons)


async def test_addressed_by_mention() -> None:
    assert await _bot_addressed(_msg("эй @yuki_stickers_bot нарисуй меня")) is True


async def test_addressed_by_name() -> None:
    assert await _bot_addressed(_msg("Юки, привет!")) is True
    assert await _bot_addressed(_msg("yuki can you draw")) is True


async def test_addressed_by_reply_to_bot() -> None:
    reply = SimpleNamespace(from_user=SimpleNamespace(id=999, is_bot=True))
    assert await _bot_addressed(_msg("ответ", reply_to=reply)) is True


async def test_not_addressed_plain_chatter() -> None:
    assert await _bot_addressed(_msg("просто болтаем о погоде")) is False


async def test_ignores_channel_autoforward_without_user() -> None:
    # Channel posts forwarded into the discussion group have no from_user.
    assert await _bot_addressed(_msg("свежий пост про юки", from_user=None)) is False


async def test_ignores_other_bots() -> None:
    other = SimpleNamespace(id=5, is_bot=True)
    assert await _bot_addressed(_msg("@yuki_stickers_bot", from_user=other)) is False


def test_router_registers_group_and_channel_handlers() -> None:
    router = build_router()
    names = [h.callback.__name__ for h in router.message.handlers]
    assert names.count("greet_group") == 2  # /start in group + addressed
    assert "cmd_start" in names
    chan = [h.callback.__name__ for h in router.channel_post.handlers]
    assert "greet_group" in chan
