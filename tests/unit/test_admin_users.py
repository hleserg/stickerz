"""Tests for the button-driven admin user management (/users + cards)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sticker_service.config import get_settings
from sticker_service.db import DEFAULT_CREDITS, Database
from sticker_service.db.repository import CREDITS_PER_PACK
from sticker_service.handlers import admin
from sticker_service.services import approvals


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ADMIN_IDS", "999")  # the acting admin
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _admin_msg() -> AsyncMock:
    m = AsyncMock()
    m.from_user = SimpleNamespace(id=999)
    return m


def _admin_cb(data: str) -> AsyncMock:
    cb = AsyncMock()
    cb.from_user = SimpleNamespace(id=999)
    cb.data = data
    cb.message = AsyncMock()
    return cb


def _buttons(markup: object) -> list[tuple[str, str | None]]:
    rows = getattr(markup, "inline_keyboard", None) or []
    return [(b.text, b.callback_data) for row in rows for b in row]


async def test_cmd_users_empty(db: Database) -> None:
    await admin.cmd_users(_admin_msg(), db)


async def test_cmd_users_lists_whitelist_with_card_buttons(db: Database) -> None:
    await db.allow(1, "alice")
    await db.allow(2, "bob")
    msg = _admin_msg()
    await admin.cmd_users(msg, db)
    markup = msg.answer.await_args.kwargs["reply_markup"]
    datas = [d for _, d in _buttons(markup)]
    assert "uc:1" in datas and "uc:2" in datas


async def test_cmd_users_rejects_non_admin(db: Database) -> None:
    await db.allow(1, "alice")
    msg = AsyncMock()
    msg.from_user = SimpleNamespace(id=12345)  # not an admin
    await admin.cmd_users(msg, db)
    msg.answer.assert_not_awaited()


async def test_user_card_shows_status_and_actions(db: Database) -> None:
    await db.allow(7, "carol")
    await db.set_credits(7, DEFAULT_CREDITS)
    text, markup = await admin._user_card(db, 7)
    assert "@carol" in text and "есть" in text
    datas = [d for _, d in _buttons(markup)]
    assert "uct:7" in datas  # toggle access
    assert f"ucg:7:{CREDITS_PER_PACK}" in datas  # +1 pack
    assert f"ucg:7:{-CREDITS_PER_PACK}" in datas  # -1 pack
    assert "users:0" in datas  # back to list
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert any("tg://user?id=7" in u for u in urls)


async def test_toggle_removes_and_grants_access(db: Database) -> None:
    await db.allow(8, "dave")
    assert await db.is_allowed(8) is True
    await admin.on_user_toggle(_admin_cb("uct:8"), db)
    assert await db.is_allowed(8) is False
    await admin.on_user_toggle(_admin_cb("uct:8"), db)
    assert await db.is_allowed(8) is True


async def test_gen_adjusts_pack_balance(db: Database) -> None:
    await db.allow(9, "erin")
    await db.set_credits(9, CREDITS_PER_PACK)  # 1 pack
    await admin.on_user_gen(_admin_cb(f"ucg:9:{CREDITS_PER_PACK}"), db)  # +1 pack
    assert await db.credits_left(9) == 2 * CREDITS_PER_PACK
    await admin.on_user_gen(_admin_cb(f"ucg:9:{-CREDITS_PER_PACK}"), db)  # -1 pack
    assert await db.credits_left(9) == CREDITS_PER_PACK


async def test_approve_user_backfills_username_to_whitelist(db: Database) -> None:
    await db.add_application(50, "frank", "из чата")
    await approvals.approve_user(db, 50)
    entry = next(e for e in await db.list_whitelist() if e.user_id == 50)
    assert entry.username == "frank"  # handle carried to the whitelist for /users
