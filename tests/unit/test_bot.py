"""Tests for the bot skeleton: dispatcher wiring and the /start handler."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher

from sticker_service.bot import build_bot
from sticker_service.handlers import build_dispatcher
from sticker_service.handlers.start import cmd_start


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
    message = AsyncMock()
    await cmd_start(message)
    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert "стикер" in text.lower()
