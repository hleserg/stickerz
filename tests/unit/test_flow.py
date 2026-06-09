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
    _prev_state,
    _progress_bar,
    _retry_kb,
    _review_text,
    _screen_for,
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


async def test_generation_gate_budget_and_credits(db: Database) -> None:
    from sticker_service.services import pricing

    await modes.set_mode(db, modes.ALPHA)
    uid = 99998
    cost = pricing.COST_NEW_PACK
    await budget.set_budget(db, 100)  # plenty
    assert await _generation_gate(db, uid, cost) is None  # default credits enough
    await db.set_credits(uid, 0)
    assert "Недостаточно" in (await _generation_gate(db, uid, cost) or "")
    await db.set_credits(uid, cost)
    await budget.set_budget(db, 0)  # global budget can't cover 2 more
    assert "приостановлено" in (await _generation_gate(db, uid, cost) or "")


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


# --- single-message wizard: back map + screen rendering ----------------------


def test_prev_state_back_map() -> None:
    assert _prev_state(NewPack.subject.state, {}) == NewPack.name.state
    assert _prev_state(NewPack.child_age.state, {}) == NewPack.subject.state
    # style goes back to subject for adults, to age for children (§B.4)
    assert _prev_state(NewPack.style.state, {"subject": "adult"}) == NewPack.subject.state
    assert _prev_state(NewPack.style.state, {"subject": "child"}) == NewPack.child_age.state
    assert _prev_state(NewPack.select_std.state, {}) == NewPack.style.state
    assert _prev_state(NewPack.ask_custom.state, {}) == NewPack.select_std.state
    assert _prev_state(NewPack.review.state, {}) == NewPack.ask_custom.state
    assert _prev_state(NewPack.photo.state, {}) is None  # no back from the entry step
    assert _prev_state(None, {}) is None


def test_prev_state_enter_custom_returns_to_its_entry_point() -> None:
    # entered from review's "add" → back to review; otherwise back to ask_custom
    assert (
        _prev_state(NewPack.enter_custom.state, {"custom_back": NewPack.review.state})
        == NewPack.review.state
    )
    assert _prev_state(NewPack.enter_custom.state, {}) == NewPack.ask_custom.state


def test_screen_for_renders_every_step(loader: StyleLoader) -> None:
    data = {"std_sel": [0, 1], "page": 0, "custom": ["Своё"]}
    for target in (
        NewPack.name.state,
        NewPack.subject.state,
        NewPack.child_age.state,
        NewPack.style.state,
        NewPack.select_std.state,
        NewPack.ask_custom.state,
        NewPack.enter_custom.state,
        NewPack.review.state,
    ):
        text, markup = _screen_for(target, data, loader)
        assert isinstance(text, str) and text
        assert markup is not None  # every step carries an inline keyboard


def test_screen_for_style_requires_loader() -> None:
    with pytest.raises(ValueError, match="StyleLoader"):
        _screen_for(NewPack.style.state, {}, None)


def test_review_text_numbers_captions_and_handles_empty() -> None:
    listing = _review_text(["Привет", "Пока"])
    assert "1. Привет" in listing and "2. Пока" in listing
    assert "ничего не выбрано" in _review_text([])


def test_retry_kb_counts_down_then_activates() -> None:
    # While counting down the button is inactive (a "wait" callback) and shows the
    # remaining seconds; at zero it becomes the live "try again" retry button.
    waiting = _retry_kb(20).inline_keyboard[0][0]
    assert "20" in waiting.text and waiting.callback_data == "retry:wait"
    ready = _retry_kb(0).inline_keyboard[0][0]
    assert "ещё раз" in ready.text.lower() and ready.callback_data == "retry:gen"


def test_review_text_shows_limit_notice_only_when_full() -> None:
    from sticker_service.services.stickers.sets import MAX_CAPTIONS

    full = [f"c{i}" for i in range(MAX_CAPTIONS)]
    assert "максимум" in _review_text(full)  # at the cap → explain the 15-per-pass limit
    assert "максимум" not in _review_text(full[:-1])  # one below → no notice


def test_single_flight_guard_blocks_reentry() -> None:
    # A second paid/publish action for the same user is refused while one runs,
    # which prevents double-tap double-spend / duplicate packs.
    from sticker_service.handlers.flow import _begin_action, _end_action

    assert _begin_action(123) is True  # first acquire wins
    assert _begin_action(123) is False  # re-entry blocked while in-flight
    _end_action(123)
    assert _begin_action(123) is True  # released → acquirable again
    _end_action(123)


def test_std_checklist_has_bulk_select_buttons() -> None:
    # The standard-caption menu offers "select all" / "clear all" shortcuts.
    from sticker_service.handlers.flow import std_checklist_kb

    markup = std_checklist_kb(selected=[0], page=0)
    callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert "stdall" in callbacks
    assert "stdclear" in callbacks
