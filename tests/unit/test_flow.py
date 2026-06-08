"""Tests for the FSM flow's invariant transitions (implicit consent, age-if-child).

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
    _alpha_gate,
    _generation_gate,
    _progress_bar,
    cmd_addto,
    cmd_cancel,
    cmd_mychars,
    cmd_mypacks,
    cmd_new,
    on_age,
    on_name,
    on_subject,
)
from sticker_service.services import budget, modes
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


async def test_cmd_new_records_consent_and_asks_photo(db: Database) -> None:
    state = _state()
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=55)
    await cmd_new(message, state, db)
    assert await db.has_consent(55) is True  # consent recorded implicitly (§15.2)
    assert await state.get_state() == NewPack.photo.state  # straight to photo
    message.answer.assert_awaited_once()


async def test_name_advances_to_subject(db: Database) -> None:
    state = _state()
    message = AsyncMock()
    message.text = "Лёшик 🎨"
    message.from_user = SimpleNamespace(id=1)
    await on_name(message, state, db)
    assert (await state.get_data())["name"] == "Лёшик 🎨"
    assert await state.get_state() == NewPack.subject.state


async def test_name_profane_is_struck(db: Database) -> None:
    state = _state()
    message = AsyncMock()
    message.text = "хуй"
    message.from_user = SimpleNamespace(id=1)
    await on_name(message, state, db)
    assert await state.get_state() != NewPack.subject.state  # rejected
    assert await db.active_strikes(1) == 1  # a strike was recorded


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


def test_progress_bar_renders_proportionally() -> None:
    assert _progress_bar(0, 3, width=10) == "▱" * 10
    assert _progress_bar(3, 3, width=10) == "▰" * 10
    mid = _progress_bar(1, 2, width=10)
    assert mid.count("▰") == 5 and mid.count("▱") == 5
    assert _progress_bar(5, 0) == "▰" * 10  # never divides by zero


async def test_cancel_clears_active_state() -> None:
    state = _state()
    await state.set_state(NewPack.photo)
    message = AsyncMock()
    await cmd_cancel(message, state)
    assert await state.get_state() is None
    assert "/new" in message.answer.await_args.args[0]


async def test_cancel_when_idle_is_noop() -> None:
    state = _state()
    message = AsyncMock()
    await cmd_cancel(message, state)
    assert "Нечего отменять" in message.answer.await_args.args[0]


async def test_alpha_gate_blocks_unapproved_then_allows(db: Database) -> None:
    await modes.set_mode(db, modes.ALPHA)
    uid = 99999  # not an admin
    hint = await _alpha_gate(db, uid)
    assert hint is not None and "заявку" in hint  # must apply first
    await db.allow(uid)
    assert await _alpha_gate(db, uid) is None  # approved → passes


async def test_generation_gate_budget_and_quota(db: Database) -> None:
    await modes.set_mode(db, modes.ALPHA)
    uid = 99998
    await budget.set_budget(db, 100)  # plenty
    assert await _generation_gate(db, uid) is None  # default quota > 0
    await db.set_generations(uid, 0)
    assert "закончились" in (await _generation_gate(db, uid) or "")
    await db.set_generations(uid, 3)
    await budget.set_budget(db, 0)  # can't cover 2 more
    assert "приостановлено" in (await _generation_gate(db, uid) or "")


async def test_mychars_empty_prompts_new(db: Database) -> None:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=1)
    await cmd_mychars(message, db)
    text = message.answer.await_args.args[0]
    assert "/new" in text


async def test_mychars_lists_saved(db: Database) -> None:
    await db.add_character(
        owner_id=1, name="Лёшик", style_id="watercolor", subject_type="adult", canonical_path="/x"
    )
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=1)
    await cmd_mychars(message, db)
    # A keyboard with the character is offered.
    assert message.answer.await_args.kwargs.get("reply_markup") is not None


async def test_mypacks_empty_prompts_new(db: Database) -> None:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=1)
    await cmd_mypacks(message, db)
    assert "/new" in message.answer.await_args.args[0]


async def test_mypacks_lists_packs(db: Database) -> None:
    char = await db.add_character(
        owner_id=1, name="A", style_id="watercolor", subject_type="adult", canonical_path="/x"
    )
    await db.add_pack(character_id=char.id, owner_id=1, set_name="d_by_bot", title="Черновик")
    await db.add_pack(
        character_id=char.id, owner_id=1, set_name="p_by_bot", title="Опубл", published=True
    )
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=1)
    await cmd_mypacks(message, db)
    assert message.answer.await_args.kwargs.get("reply_markup") is not None


async def test_addto_empty_prompts_new(db: Database) -> None:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=1)
    await cmd_addto(message, db)
    assert "/new" in message.answer.await_args.args[0]


async def test_addto_lists_packs(db: Database) -> None:
    char = await db.add_character(
        owner_id=1, name="A", style_id="watercolor", subject_type="adult", canonical_path="/x"
    )
    await db.add_pack(
        character_id=char.id, owner_id=1, set_name="s_by_bot", title="Мой пак", published=True
    )
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=1)
    await cmd_addto(message, db)
    assert message.answer.await_args.kwargs.get("reply_markup") is not None
