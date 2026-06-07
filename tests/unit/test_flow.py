"""Tests for the FSM flow's invariant transitions (consent-first, age-if-child).

flow.py is the aiogram I/O shell (excluded from coverage like bot.py/cli.py),
but the invariant-bearing transitions are verified here.
"""

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
from sticker_service.db import Database
from sticker_service.handlers.flow import (
    NewPack,
    cmd_new,
    on_age,
    on_consent,
    on_name,
    on_subject,
)
from sticker_service.services.canonical import StyleLoader


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def loader() -> StyleLoader:
    return StyleLoader(get_settings().styles_dir)


async def test_cmd_new_asks_consent_first() -> None:
    state = _state()
    message = AsyncMock()
    await cmd_new(message, state)
    assert await state.get_state() == NewPack.consent.state
    message.answer.assert_awaited_once()


async def test_consent_recorded_before_photo(db: Database) -> None:
    state = _state()
    await state.set_state(NewPack.consent)
    callback = AsyncMock()
    callback.from_user = SimpleNamespace(id=55)
    await on_consent(callback, state, db)
    assert await db.has_consent(55) is True  # consent fact persisted (§15.2)
    assert await state.get_state() == NewPack.photo.state  # only now ask for photo
    callback.answer.assert_awaited_once()


async def test_name_advances_to_subject() -> None:
    state = _state()
    message = AsyncMock()
    message.text = "Лёшик 🎨"
    await on_name(message, state)
    assert (await state.get_data())["name"] == "Лёшик 🎨"
    assert await state.get_state() == NewPack.subject.state


async def test_child_is_asked_age(loader: StyleLoader) -> None:
    state = _state()
    callback = AsyncMock()
    callback.data = "subject:child"
    await on_subject(callback, state, loader)
    assert await state.get_state() == NewPack.child_age.state  # age asked for child
    assert (await state.get_data())["subject"] == "child"


async def test_adult_skips_age(loader: StyleLoader) -> None:
    state = _state()
    callback = AsyncMock()
    callback.data = "subject:adult"
    await on_subject(callback, state, loader)
    data = await state.get_data()
    assert data["subject"] == "adult"
    assert data["child_age"] is None  # adult never carries an age (§B.4)
    assert await state.get_state() == NewPack.style.state  # straight to style


async def test_age_selection_advances_to_style(loader: StyleLoader) -> None:
    state = _state()
    callback = AsyncMock()
    callback.data = "age:6"
    await on_age(callback, state, loader)
    assert (await state.get_data())["child_age"] == 6
    assert await state.get_state() == NewPack.style.state
