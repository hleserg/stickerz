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
    await cmd_new(message, state, db, AsyncMock())
    assert await db.has_consent(55) is True  # consent recorded implicitly (§15.2)
    assert await state.get_state() == NewPack.photo.state  # straight to photo
    message.answer.assert_awaited_once()


async def test_review_stickers_never_loads_target_pack_in_extend_mode() -> None:
    """REGRESSION (review blocker): in extend mode pack_id is the TARGET pack —
    falling back to it would re-publish the pack's own stickers and double the
    set from a stale review button. Must resolve to empty instead."""
    from sticker_service.handlers.flow import _review_stickers

    orchestrator = AsyncMock()
    # Mid-extend state: pack_id seeded, no scratch yet (pre-generation).
    data = {"mode": "extend", "pack_id": 17}
    assert await _review_stickers(orchestrator, data) == []
    orchestrator.load_pack_stickers.assert_not_awaited()


async def test_review_stickers_resolution_order() -> None:
    from sticker_service.handlers.flow import _review_stickers

    orchestrator = AsyncMock()
    orchestrator.load_scratch.return_value = [(b"s", "🙂")]
    orchestrator.load_pack_stickers.return_value = [(b"d", "👍")]
    # Legacy FSM bytes win (pre-deploy in-flight reviews).
    legacy = {"stickers": [(b"raw", "🔥")], "scratch_path": "/x", "pack_id": 1}
    assert await _review_stickers(orchestrator, legacy) == [(b"raw", "🔥")]
    # Extend review: scratch wins, no pack fallback even with pack_id present.
    extend = {"mode": "extend", "scratch_path": "/data/scratch/1_a", "pack_id": 1}
    assert await _review_stickers(orchestrator, extend) == [(b"s", "🙂")]
    # Fresh/reuse: the draft pack holds exactly the reviewed stickers.
    fresh = {"mode": "fresh", "pack_id": 2}
    assert await _review_stickers(orchestrator, fresh) == [(b"d", "👍")]


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
    await cmd_cancel(message, state, AsyncMock())
    assert await state.get_state() is None
    assert "/new" in message.answer.await_args.args[0]


async def test_cancel_when_idle_is_noop() -> None:
    state = _state()
    message = AsyncMock()
    await cmd_cancel(message, state, AsyncMock())
    assert "Нечего отменять" in message.answer.await_args.args[0]


async def test_cancel_drops_review_scratch() -> None:
    state = _state()
    await state.set_state(NewPack.publish)
    await state.update_data(scratch_path="/data/scratch/1_abc")
    orchestrator = AsyncMock()
    await cmd_cancel(AsyncMock(), state, orchestrator)
    orchestrator.drop_scratch.assert_awaited_once_with("/data/scratch/1_abc")


def test_canonical_progress_text_tells_real_stage() -> None:
    # While later steps run → style line with the bar; the final callback lands
    # right after the last advisory gate → the "checking the drawing" line.
    from sticker_service.handlers.flow import _canonical_progress_text

    mid = _canonical_progress_text(1, 3)
    assert "Придаю рисунку" in mid and "1/3" in mid and "▰" in mid
    assert "Проверяю рисунок" in _canonical_progress_text(3, 3)
    assert "Проверяю рисунок" in _canonical_progress_text(1, 1)  # single-step style


async def test_enter_captions_extend_drops_stale_fresh_state(db: Database) -> None:
    from sticker_service.handlers.flow import _enter_captions

    state = _state()
    # Leftover from a previous, unfinished /new flow sitting in the persistent FSM.
    await state.update_data(
        mode="fresh", name="Серг", photo=b"X", style_id="watercolor", subject="adult"
    )
    callback = AsyncMock()
    callback.data = "extend:7"
    callback.from_user = SimpleNamespace(id=1)
    await _enter_captions(callback, state, db, AsyncMock(), mode="extend", pack_id=7)
    data = await state.get_data()
    assert data["mode"] == "extend" and data["pack_id"] == 7
    # Stale fresh-flow data is wiped so it can't hijack publish into a new pack.
    assert "name" not in data and "photo" not in data and "style_id" not in data


async def test_enter_captions_fresh_keeps_collected_state(db: Database) -> None:
    from sticker_service.handlers.flow import _enter_captions

    state = _state()
    await state.update_data(photo=b"X", name="Лёша", style_id="watercolor", subject="adult")
    callback = AsyncMock()
    callback.data = "style:watercolor"
    callback.from_user = SimpleNamespace(id=1)
    await _enter_captions(callback, state, db, AsyncMock(), mode="fresh", style_id="watercolor")
    data = await state.get_data()
    assert data["mode"] == "fresh"
    assert data["photo"] == b"X" and data["name"] == "Лёша"  # collected data preserved


async def test_enter_captions_seeds_full_standard_default(db: Database) -> None:
    # The pre-fill is the plain full standard block again; the meme pool plays
    # through the 🎲 button on the idea-input step, not through pre-seeded items.
    from sticker_service.handlers.flow import _enter_captions
    from sticker_service.services.stickers import STANDARD_BLOCK

    state = _state()
    callback = AsyncMock()
    callback.data = "style:watercolor"
    callback.from_user = SimpleNamespace(id=1)
    await _enter_captions(callback, state, db, AsyncMock(), mode="fresh", style_id="watercolor")
    data = await state.get_data()
    assert data["std_sel"] == list(range(len(STANDARD_BLOCK)))
    assert data["custom"] == []
    assert "meme_items" not in data and "meme_sel" not in data


async def test_enter_captions_in_alpha_seeds_wallet(db: Database) -> None:
    # In alpha the FSM carries {"alpha": True, "bal": credits} so the pure
    # screen renderer can show the price/balance line without a DB handle.
    from sticker_service.db import DEFAULT_CREDITS
    from sticker_service.handlers.flow import _enter_captions

    await modes.set_mode(db, modes.ALPHA)
    state = _state()
    callback = AsyncMock()
    callback.from_user = SimpleNamespace(id=424242)
    await _enter_captions(callback, state, db, AsyncMock(), mode="reuse", character_id=3)
    data = await state.get_data()
    assert data["alpha"] is True and data["bal"] == DEFAULT_CREDITS


def test_money_line_shows_price_and_balance_in_alpha_only() -> None:
    from sticker_service.handlers.flow import _money_line

    assert _money_line({}) is None  # debug mode / admin → no money talk
    line = _money_line({"alpha": True, "mode": "fresh", "bal": 5}) or ""
    assert "спишет 1 пак" in line and "2.5" in line and "бесплатны" in line
    half = _money_line({"alpha": True, "mode": "extend"}) or ""
    assert "0.5" in half  # half-price actions say so even without a balance


def test_screens_carry_the_money_line_in_alpha() -> None:
    from sticker_service.handlers.flow import NewPack, _screen_for

    wallet = {"alpha": True, "mode": "fresh", "bal": 6, "std_sel": [0], "custom": []}
    for target in (NewPack.select_std.state, NewPack.review.state):
        text, _markup = _screen_for(target, wallet, None)
        assert "💸" in text and "💎" in text
        plain, _markup = _screen_for(target, {"std_sel": [0], "custom": []}, None)
        assert "💸" not in plain  # outside alpha the screens stay money-free


def test_enter_custom_screen_offers_random_prompt_button() -> None:
    from sticker_service.handlers.flow import NewPack, _screen_for

    _text, markup = _screen_for(NewPack.enter_custom.state, {}, None)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.callback_data == "randidea" for b in buttons)
    assert any("Случайный промт" in b.text for b in buttons)


def test_random_idea_kb_offers_take_reroll_and_copy() -> None:
    from sticker_service.handlers.flow import random_idea_kb

    markup = random_idea_kb("Пьёт кофе. Подпись: «Первый глоток.»")
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.callback_data == "randtake" for b in buttons)
    assert any(b.callback_data == "randidea" for b in buttons)  # reroll
    copies = [b for b in buttons if b.copy_text is not None]
    assert len(copies) == 1  # paste-into-input helper
    assert copies[0].copy_text.text.startswith("Пьёт кофе.")


async def test_random_idea_roll_then_take_appends_custom(db: Database) -> None:
    from sticker_service.handlers.flow import on_random_idea, on_random_take

    state = _state()
    await state.update_data(std_sel=[0], custom=[])
    callback = AsyncMock()
    callback.data = "randidea"
    callback.message = None  # no real message → screen edit is skipped, state still set
    await on_random_idea(callback, state, db)
    item = (await state.get_data())["rand_idea"]
    assert "Подпись: «" in item or item.endswith("Без подписи.")  # prompt-ready
    take = AsyncMock()
    take.data = "randtake"
    await on_random_take(take, state)
    data = await state.get_data()
    assert data["custom"] == [item]
    assert data["rand_idea"] is None  # consumed


async def test_random_take_respects_the_15_cap() -> None:
    from sticker_service.handlers.flow import on_random_take

    state = _state()
    await state.update_data(
        std_sel=list(range(13)), custom=["а", "б"], rand_idea="Идея. Без подписи."
    )
    callback = AsyncMock()
    await on_random_take(callback, state)
    data = await state.get_data()
    assert data["custom"] == ["а", "б"]  # unchanged — the cap held
    assert "15" in callback.answer.await_args.args[0]  # unobtrusive toast


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


def test_style_kb_hides_experimental_behind_shelf() -> None:
    # The main picker lists polished styles + a shelf entry, and never the
    # experimental styles themselves (they live behind the shelf).
    from sticker_service.config import get_settings
    from sticker_service.handlers.flow import style_kb
    from sticker_service.services.canonical import StyleLoader

    markup = style_kb(StyleLoader(get_settings().styles_dir))
    callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert "styles:exp" in callbacks  # shelf entry
    assert "style:watercolor" in callbacks  # polished style listed
    assert "style:minecraft" not in callbacks  # experimental hidden here


def test_experimental_shelf_lists_exp_styles_with_way_back() -> None:
    from sticker_service.config import get_settings
    from sticker_service.handlers.flow import style_experimental_kb
    from sticker_service.services.canonical import StyleLoader

    markup = style_experimental_kb(StyleLoader(get_settings().styles_dir))
    callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert "style:minecraft" in callbacks  # experimental style is selectable
    assert "styles:main" in callbacks  # and can return to polished styles
    assert "style:watercolor" not in callbacks  # polished not duplicated here


async def test_only_first_admin_has_unlimited_generations(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner (first admin) generates for free; other admins are billed and
    # gated like ordinary alpha testers.
    from sticker_service.config import get_settings
    from sticker_service.handlers.flow import _generation_gate
    from sticker_service.services import budget

    monkeypatch.setenv("APP_ADMIN_IDS", "1,2")  # first admin = 1
    get_settings.cache_clear()
    try:
        await modes.set_mode(db, modes.ALPHA)
        await budget.set_budget(db, 100)
        await db.set_credits(1, 0)  # first admin, no packs…
        await db.set_credits(2, 0)  # second admin, no packs
        assert await _generation_gate(db, 1, 2) is None  # …still unlimited
        assert await _generation_gate(db, 2, 2) is not None  # gated like a tester
    finally:
        get_settings.cache_clear()


async def test_drain_generations_waits_out_inflight_tasks() -> None:
    # The shutdown drain must await detached generation tasks (aiogram doesn't),
    # and report how many missed the deadline so the log shows what was cut.
    import asyncio

    from sticker_service.handlers import flow

    assert await flow.drain_generations(timeout=0.1) == 0  # nothing in flight

    quick = asyncio.create_task(asyncio.sleep(0.01))
    slow = asyncio.create_task(asyncio.sleep(30))
    flow._bg_tasks.update({quick, slow})
    try:
        pending = await flow.drain_generations(timeout=0.3)
    finally:
        slow.cancel()
        flow._bg_tasks.clear()
    assert pending == 1  # quick finished inside the window, slow was still running


async def test_status_line_notice_reverts_to_stage() -> None:
    # A retry/fallback notice is a moment, not a state: it shows briefly, then
    # the line returns to what is actually happening (the current stage).
    import asyncio
    from unittest.mock import AsyncMock

    from sticker_service.handlers.flow import StatusLine

    msg = AsyncMock()
    status = StatusLine(msg)
    status.NOTICE_SECONDS = 0.05
    await status.stage("🖼️ Рисую лист стикеров…")
    await status.notice("⚖️ Беру менее загруженную модель…")
    assert msg.edit_text.call_args.args[0].startswith("⚖️")
    await asyncio.sleep(0.15)  # revert fires
    assert msg.edit_text.call_args.args[0] == "🖼️ Рисую лист стикеров…"


async def test_status_line_newer_stage_cancels_revert() -> None:
    # If a new stage arrives while a notice shows, the revert must not stomp it.
    import asyncio
    from unittest.mock import AsyncMock

    from sticker_service.handlers.flow import StatusLine

    msg = AsyncMock()
    status = StatusLine(msg)
    status.NOTICE_SECONDS = 0.05
    await status.stage("Этап 1")
    await status.notice("🔁 Повторяю…")
    await status.stage("Этап 2")  # newer than the notice
    await asyncio.sleep(0.15)
    assert msg.edit_text.call_args.args[0] == "Этап 2"  # not reverted to "Этап 1"


async def test_status_line_heartbeat_appends_elapsed_when_idle() -> None:
    # Nothing changed for a while → the line gains elapsed time, so a long
    # model call visibly ticks instead of looking frozen.
    import asyncio
    from unittest.mock import AsyncMock

    from sticker_service.handlers.flow import StatusLine

    msg = AsyncMock()
    status = StatusLine(msg)
    status.HEARTBEAT_SECONDS = 0.05
    await status.stage("🎨 Превращаю фото в рисунок…")
    status.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        status.stop()
    last = msg.edit_text.call_args.args[0]
    assert last.startswith("🎨 Превращаю фото в рисунок… · уже")


def test_std_buttons_show_exact_prompt_lines() -> None:
    # Full transparency (owner's rule): a standard-sticker button shows exactly
    # the line that will go into the sheet prompt — «…» for replicas, a bare
    # word for emotions.
    from sticker_service.handlers.flow import std_checklist_kb
    from sticker_service.services.stickers import PER_PAGE, STANDARD_BLOCK, prompt_idea

    texts: list[str] = []
    pages = (len(STANDARD_BLOCK) + PER_PAGE - 1) // PER_PAGE
    for page in range(pages):
        markup = std_checklist_kb(selected=[], page=page)
        texts += [
            b.text
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data and b.callback_data.startswith("std:")
        ]
    assert [t.removeprefix("⬜ ") for t in texts] == [prompt_idea(c) for c in STANDARD_BLOCK]
    assert "⬜ «Привет!»" in texts  # реплика — в кавычках
    assert "⬜ Грустно" in texts  # эмоция — словом
    assert '⬜ "Ок!" 👌😉' in texts  # владельская строка дословно
    assert '⬜ 👍"Класс!"' in texts
    assert "⬜ 😎 Я крутой!" in texts


def test_wizard_callback_prefix_matcher() -> None:
    # The stale-button catch-all must cover every forward-wizard prefix and
    # nothing from other routers (admin/apply/users).
    from sticker_service.handlers.flow import _is_wizard_callback

    for live in (
        "style:watercolor",
        "styles:exp",
        "subject:adult",
        "age:5",
        "std:3",
        "stdall",
        "stddone",
        "cust:yes",
        "randidea",
        "rev:create",
        "rem:1",
        "retry:gen",
        "pub:dl",
    ):
        assert _is_wizard_callback(live), live
    for foreign in (
        "apply:new",
        "users:0",
        "uc:5",
        "uct:5",
        "mode:alpha",
        "nav:back",
        "char:1",
        "pk:2",
        None,
        "",
    ):
        assert not _is_wizard_callback(foreign), foreign


async def test_stale_style_tap_is_answered_not_crashed() -> None:
    # Regression (live, tester 8103588997): tapping a style button on a DEAD
    # wizard message walked the flow into "fresh mode needs photo…". With state
    # filters the tap lands on the catch-all: kind alert + keyboard stripped.
    from unittest.mock import AsyncMock

    from aiogram.types import Message

    from sticker_service.handlers.flow import on_stale_wizard

    callback = AsyncMock()
    callback.data = "style:watercolor"
    callback.message = AsyncMock(spec=Message)
    await on_stale_wizard(callback)
    callback.answer.assert_awaited_once()
    assert "устарела" in callback.answer.call_args.args[0]
    callback.message.edit_reply_markup.assert_called_once_with(reply_markup=None)
