"""Tests for application approval — shared action + alpha auto-approve seats."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from sticker_service.config import get_settings
from sticker_service.db import DEFAULT_CREDITS, Database
from sticker_service.handlers.apply import on_apply_source
from sticker_service.services import approvals, modes


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def _state(user_id: int = 1) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


async def test_approve_user_grants_everything(db: Database) -> None:
    await db.add_application(7, "tester", "from a friend")
    await approvals.approve_user(db, 7)
    app = await db.get_application(7)
    assert app is not None and app.status == "approved"
    assert await db.is_allowed(7) is True
    assert await db.credits_left(7) == DEFAULT_CREDITS


async def test_auto_approve_takes_seats_then_stops(db: Database) -> None:
    await modes.set_mode(db, modes.ALPHA)
    get_settings.cache_clear()
    limit = get_settings().alpha_auto_approve_limit
    assert limit == 30  # the product ask: first 30 testers walk right in
    # Fill all but one seat with prior approvals (manual ones count too).
    for uid in range(100, 100 + limit - 1):
        await db.add_application(uid, None, "x")
        await approvals.approve_user(db, uid)

    await db.add_application(7, "last", "x")
    assert await approvals.maybe_auto_approve(db, 7) is True  # seat 30/30
    assert await db.is_allowed(7) is True

    await db.add_application(8, "late", "x")
    assert await approvals.maybe_auto_approve(db, 8) is False  # seats are full
    app = await db.get_application(8)
    assert app is not None and app.status == "pending"  # waits for the admin


async def test_auto_approve_inactive_outside_alpha(db: Database) -> None:
    assert await modes.get_mode(db) == modes.DEBUG
    await db.add_application(7, None, "x")
    assert await approvals.maybe_auto_approve(db, 7) is False


async def test_auto_approve_disabled_by_zero_limit(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await modes.set_mode(db, modes.ALPHA)
    monkeypatch.setenv("APP_ALPHA_AUTO_APPROVE_LIMIT", "0")
    get_settings.cache_clear()
    try:
        await db.add_application(7, None, "x")
        assert await approvals.maybe_auto_approve(db, 7) is False
    finally:
        get_settings.cache_clear()


async def test_application_flow_welcomes_auto_approved_user(db: Database) -> None:
    await modes.set_mode(db, modes.ALPHA)
    get_settings.cache_clear()
    message = AsyncMock()
    message.text = "увидел у друга"
    message.from_user = SimpleNamespace(id=42, username="happy")
    state = _state(42)

    await on_apply_source(message, state, db)

    answer = message.answer.await_args.args[0]
    assert "Рады приветствовать" in answer  # the welcome, not "ждите"
    assert await db.is_allowed(42) is True
    assert await db.credits_left(42) == DEFAULT_CREDITS


async def test_application_flow_keeps_pending_when_seats_full(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    await modes.set_mode(db, modes.ALPHA)
    monkeypatch.setenv("APP_ALPHA_AUTO_APPROVE_LIMIT", "0")
    get_settings.cache_clear()
    try:
        message = AsyncMock()
        message.text = "из чата"
        message.from_user = SimpleNamespace(id=43, username=None)
        await on_apply_source(message, _state(43), db)
        assert "Заявка отправлена" in message.answer.await_args.args[0]
        assert await db.is_allowed(43) is False
    finally:
        get_settings.cache_clear()
