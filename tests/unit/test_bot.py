"""Tests for the bot skeleton: dispatcher wiring, scoped menus, /start handler."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher

from sticker_service.bot import build_bot, commands_for
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


def test_scoped_menus_layer_by_role() -> None:
    user = [c.command for c in commands_for(admin=False, owner=False)]
    admin = [c.command for c in commands_for(admin=True, owner=False)]
    owner = [c.command for c in commands_for(admin=True, owner=True)]
    # Regular users never see the admin toolbox.
    assert "new" in user and "stats" not in user and "mode" not in user
    # Every admin gets the toolbox on top, plus the normal user commands.
    assert admin[0] == "users" and "stats" in admin and "waiting" in admin and "new" in admin
    assert "mode" not in admin  # mode/budget stay owner-only
    # The first admin additionally gets the owner switches.
    assert "mode" in owner and "setbudget" in owner and "stats" in owner
    # Every registered admin command actually exists in the admin router.
    from sticker_service.handlers.admin import build_router

    registered = {f.callback.__name__ for f in build_router().message.handlers}  # e.g. cmd_stats
    for command in set(admin) - set(user):
        assert f"cmd_{command}" in registered


async def test_set_commands_scopes_default_admin_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.types import BotCommandScopeChat

    from sticker_service.bot import _set_commands
    from sticker_service.config import Settings

    settings = Settings(admin_ids="10,20", _env_file=None)  # type: ignore[call-arg]
    bot = AsyncMock()
    await _set_commands(bot, settings)

    from aiogram.types import BotCommandScopeAllGroupChats

    calls = bot.set_my_commands.await_args_list
    assert len(calls) == 4  # default + all-group-chats + two admin chats
    default_cmds = [c.command for c in calls[0].args[0]]
    assert "stats" not in default_cmds  # the public menu stays clean
    group_call = next(
        c for c in calls if isinstance(c.kwargs.get("scope"), BotCommandScopeAllGroupChats)
    )
    assert [c.command for c in group_call.args[0]] == ["start"]  # groups: only /start
    by_chat = {
        c.kwargs["scope"].chat_id: [cmd.command for cmd in c.args[0]]
        for c in calls
        if isinstance(c.kwargs.get("scope"), BotCommandScopeChat)
    }
    assert "mode" in by_chat[10] and "stats" in by_chat[10]  # first admin = owner
    assert "mode" not in by_chat[20] and "stats" in by_chat[20]  # other admin


async def test_set_commands_survives_a_403_admin_chat() -> None:
    """An admin who never opened the bot must not break menu setup for the rest."""
    from sticker_service.bot import _set_commands
    from sticker_service.config import Settings

    settings = Settings(admin_ids="10,20", _env_file=None)  # type: ignore[call-arg]
    bot = AsyncMock()

    async def _flaky(commands: object, scope: object = None) -> None:
        if getattr(scope, "chat_id", None) == 10:
            raise RuntimeError("Bad Request: chat not found")

    bot.set_my_commands.side_effect = _flaky
    await _set_commands(bot, settings)  # must not raise
    assert bot.set_my_commands.await_count == 4  # default + group scope + both admins


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
