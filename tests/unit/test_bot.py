"""Tests for the bot skeleton: dispatcher wiring and the /start handler."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher

from sticker_service.bot import build_bot
from sticker_service.handlers import build_dispatcher
from sticker_service.handlers.start import cmd_help, cmd_rules, cmd_start


def test_build_bot_requires_token() -> None:
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        build_bot(token="")


def test_build_bot_returns_bot() -> None:
    bot = build_bot(token="123456:AA-test_Token_value_1234567890abcdefg")
    assert isinstance(bot, Bot)


def test_build_dispatcher_registers_start_router() -> None:
    dp = build_dispatcher()
    assert isinstance(dp, Dispatcher)
    assert "start" in [r.name for r in dp.sub_routers]


async def test_cmd_start_answers() -> None:
    from types import SimpleNamespace

    from sticker_service.db import Database

    db = await Database.connect(":memory:")
    try:
        message = AsyncMock()
        message.from_user = SimpleNamespace(id=1)
        await cmd_start(message, db)
        message.answer.assert_awaited_once()
        text = message.answer.await_args.args[0]
        assert "стикер" in text.lower()
        assert await db.has_events(1)  # start event recorded
    finally:
        await db.close()


async def test_cmd_help_lists_capabilities_and_prices() -> None:
    from types import SimpleNamespace

    from sticker_service.db import Database

    db = await Database.connect(":memory:")
    try:
        message = AsyncMock()
        message.from_user = SimpleNamespace(id=1)
        await cmd_help(message, db)
        text = message.answer.await_args.args[0]
        assert "/new" in text and "пак" in text.lower()  # capabilities + pricing
        assert "/balance" in text
    finally:
        await db.close()


async def test_cmd_rules_answers() -> None:
    message = AsyncMock()
    await cmd_rules(message)
    assert "Правила" in message.answer.await_args.args[0]
