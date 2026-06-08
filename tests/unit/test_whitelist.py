"""Tests for the whitelist middleware and admin commands (§11.1, §B.4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.filters import CommandObject

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.handlers import build_dispatcher
from sticker_service.handlers.admin import cmd_allow, cmd_deny
from sticker_service.handlers.middleware import WhitelistMiddleware


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def _user(uid: int, username: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=uid, username=username)


# --- middleware --------------------------------------------------------------


async def test_blocks_unlisted_user(db: Database) -> None:
    mw = WhitelistMiddleware(db)
    handler = AsyncMock()
    event = AsyncMock()
    result = await mw(handler, event, {"event_from_user": _user(5)})
    handler.assert_not_called()
    event.answer.assert_awaited_once()
    assert result is None


async def test_allows_whitelisted_user(db: Database) -> None:
    await db.set_config("mode", "prod")
    await db.allow(5)
    mw = WhitelistMiddleware(db)
    handler = AsyncMock(return_value="ok")
    event = AsyncMock()
    result = await mw(handler, event, {"event_from_user": _user(5)})
    handler.assert_awaited_once()
    assert result == "ok"


async def test_debug_mode_blocks_non_admin(db: Database) -> None:
    # Default mode is debug → even a whitelisted non-admin gets the dev notice.
    await db.allow(5)
    mw = WhitelistMiddleware(db)
    handler = AsyncMock()
    event = AsyncMock()
    await mw(handler, event, {"event_from_user": _user(5)})
    handler.assert_not_called()
    event.answer.assert_awaited_once()


async def test_admin_passes_and_is_auto_added(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ADMIN_IDS", "99")
    get_settings.cache_clear()
    mw = WhitelistMiddleware(db)
    handler = AsyncMock()
    await mw(handler, AsyncMock(), {"event_from_user": _user(99, "boss")})
    handler.assert_awaited_once()
    assert await db.is_allowed(99) is True  # auto-added


async def test_banned_user_blocked(db: Database) -> None:
    from datetime import UTC, datetime, timedelta

    await db.set_config("mode", "prod")
    await db.allow(5)
    await db.set_ban(5, datetime.now(UTC) + timedelta(hours=1))
    mw = WhitelistMiddleware(db)
    handler = AsyncMock()
    event = AsyncMock()
    await mw(handler, event, {"event_from_user": _user(5)})
    handler.assert_not_called()  # blocked by active ban
    event.answer.assert_awaited_once()


async def test_admin_exempt_from_ban(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("APP_ADMIN_IDS", "9")
    get_settings.cache_clear()
    await db.set_ban(9, datetime.now(UTC) + timedelta(hours=1))
    mw = WhitelistMiddleware(db)
    handler = AsyncMock()
    await mw(handler, AsyncMock(), {"event_from_user": _user(9)})
    handler.assert_awaited_once()  # admins bypass bans


async def test_service_update_without_user_passes(db: Database) -> None:
    mw = WhitelistMiddleware(db)
    handler = AsyncMock()
    await mw(handler, AsyncMock(), {})
    handler.assert_awaited_once()


# --- admin commands ----------------------------------------------------------


async def test_allow_command_adds(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ADMIN_IDS", "1")
    get_settings.cache_clear()
    message = AsyncMock()
    message.from_user = _user(1)
    await cmd_allow(message, CommandObject(args="5"), db)
    assert await db.is_allowed(5) is True
    message.answer.assert_awaited_once()


async def test_deny_command_removes(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ADMIN_IDS", "1")
    get_settings.cache_clear()
    await db.allow(5)
    message = AsyncMock()
    message.from_user = _user(1)
    await cmd_deny(message, CommandObject(args="5"), db)
    assert await db.is_allowed(5) is False


async def test_non_admin_cannot_allow(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ADMIN_IDS", "1")
    get_settings.cache_clear()
    message = AsyncMock()
    message.from_user = _user(777)  # not an admin
    await cmd_allow(message, CommandObject(args="5"), db)
    assert await db.is_allowed(5) is False
    message.answer.assert_not_called()


async def test_allow_command_rejects_non_numeric(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ADMIN_IDS", "1")
    get_settings.cache_clear()
    message = AsyncMock()
    message.from_user = _user(1)
    await cmd_allow(message, CommandObject(args="@bob"), db)
    message.answer.assert_awaited_once()  # usage hint, nothing added


async def test_build_dispatcher_with_db_registers_admin(db: Database) -> None:
    dp = build_dispatcher(db)
    assert "admin" in [r.name for r in dp.sub_routers]
    assert dp["db"] is db
