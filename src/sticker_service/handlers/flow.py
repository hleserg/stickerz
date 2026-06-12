"""Conversational pack-building flow (§3.1): new/existing character, extend.

This is the aiogram I/O shell that drives the user through photo → params →
style → canonical → confirm → published pack, and the "add to pack" branch.
Business logic lives in the tested service layer (orchestrator, engine,
postprocess); this module wires Telegram interaction to it.

UX model — a single "wizard message" per flow:
- The bot keeps ONE message ("wizard message", its id stored in FSM) and EDITS
  it at every step instead of posting a new message each time, so a whole run
  reads as one updating card rather than a wall of jumping messages.
- ``_screen_for`` is the single source of truth that renders any step to
  ``(text, keyboard)``; forward steps and the ``⬅️ Назад`` button both go through
  it, so navigation can never drift out of sync.
- The user's own typed messages (name, captions) are deleted to keep the chat
  clean; a sent photo is kept, and the wizard is then re-sent *below* the photo
  so the controls sit under the image, not above it.

Invariants enforced here and covered by tests:
- consent (fact + timestamp) is recorded implicitly when /new starts (§15.2);
- age is asked ONLY for children, never for adults (§B.4 / {age_clause}).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services import analytics, budget, modes, photo_check, pricing
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.models import errors as model_errors
from sticker_service.services.models.base import notice_sink
from sticker_service.services.moderation import caption_rejection_reason
from sticker_service.services.orchestrator import Orchestrator
from sticker_service.services.postprocess import bundle_zip, compose_preview
from sticker_service.services.publish import PackFullError, remaining_capacity
from sticker_service.services.publish.publisher import StickerInput
from sticker_service.services.stickers import MAX_CAPTIONS, STANDARD_BLOCK, selected_captions
from sticker_service.services.strikes import register_strike

logger = logging.getLogger(__name__)

# Live status lines for every real pipeline stage — the message changes as the
# work actually progresses, so the bot visibly "thinks" instead of idling on one
# static caption. Keys match the orchestrator's StageCallback labels plus the
# canonical-phase labels rendered by _canonical_progress_text.
_STAGE_TEXT = {
    "photo_to_art": "🎨 Превращаю фото в рисунок…",
    "style": "✨ Придаю рисунку выбранный стиль…",
    "gate": "🧐 Проверяю рисунок…",
    "clean": "🪄 Убираю фон — делаю прозрачным…",
    "slice": "✂️ Режу лист на отдельные стикеры…",
    "emoji": "🎭 Подбираю каждому стикеру эмодзи…",
    "preview": "🧩 Собираю превью…",
    "publish": "📦 Публикую пак в Telegram…",
}

# The sheet call is the longest single model call (minutes), so its stage is
# animated instead of static: StatusLine rotates these frames (one per
# FRAME_SECONDS, with the elapsed timer) — the growing dots 1→2→3 are the
# owner's requested rhythm, keep them in sync with the frame order.
_SHEET_FRAMES = (
    "🖼️ Рисую лист стикеров: позы.",
    "🖼️ Рисую лист стикеров: эмоции..",
    "🖼️ Рисую лист стикеров: подписи…",
)


def _dot_frames(text: str) -> tuple[str, ...]:
    """Expand a trailing «…» into rotating ``.`` / ``..`` / ``…`` frames.

    The owner's rhythm (13.06): every status that can hang ends with «…», so
    it must visibly tick (one frame per FRAME_SECONDS) instead of freezing.
    Text without the trailing ellipsis is a single static frame; «…» anywhere
    else (e.g. followed by a progress bar) stays static on purpose.
    """
    if not text.endswith("…"):
        return (text,)
    base = text.removesuffix("…")
    return (f"{base}.", f"{base}..", f"{base}…")


# Live notices when a model call is re-issued or fails over (overload/timeout),
# so a long wait surfaces as a moving status line instead of a frozen message.
# Keys match the model layer's NoticeCallback ("retry"/"fallback").
_NOTICE_TEXT = {
    "retry": "🔁 ИИ-художник занят — повторяю запрос…",
    "fallback": "⚖️ Беру менее загруженную модель…",
}

# --- screen copy (single source of truth for step text) ----------------------

_TXT_PHOTO = (
    "Создаём новый пак. Пришли фото человека.\n\n"
    "🐞 Заметишь любой косяк — пожалуйста, напиши /report с подробностями "
    "(что делал, что не так): это очень помогает улучшать бота."
)
_TXT_NAME = "Как назвать пак/персонажа? (можно кириллицу и эмодзи)\n\nНапиши ответ сообщением."
_TXT_SUBJECT = "Это взрослый или ребёнок?"
_TXT_AGE = "Сколько лет ребёнку?"
_TXT_STYLE = "Выбери стиль:"
_TXT_STYLE_EXP = (
    "Тут лежат экспериментальные стили, пока ещё не отлаженные, но уже могут быть "
    "интересными. Но помните: выбирая такой стиль, вы соглашаетесь на то, что "
    "последствия могут быть непредсказуемыми."
)
_TXT_CAPTIONS = (
    "Выберите стандартные стикеры для набора. Дальше можно добавить свои "
    "или взять 🎲 случайную идею — всего не больше 15 (один лист)."
)
_TXT_NEW_OR_ADDTO = "Новый пак: /new · Дополнить существующий: /addto (вдвое дешевле)"
_TXT_ASK_CUSTOM = (
    "Хотите добавить свои стикеры? Можно описать идею своими словами или взять 🎲 случайную."
)
_TXT_ENTER_CUSTOM = (
    "Опишите идею стикера сообщением — что на нём происходит (поза, эмоция, одежда, сценка). "
    'Нужна надпись? Напишите ее в кавычках, например: «Огонь!» или "С днём рождения!".'
)


def _friendly_error(exc: Exception) -> str:
    """Turn a backend exception into a short user-facing message (shared taxonomy)."""
    if isinstance(exc, PackFullError):  # already a ready RU sentence (remaining slots)
        return str(exc)
    return model_errors.user_message(exc)


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    """Render a ``▰▰▱▱▱`` bar for a done/total step count."""
    total = max(total, 1)
    filled = round(width * max(0, min(done, total)) / total)
    return "▰" * filled + "▱" * (width - filled)


def _canonical_progress_text(done: int, total: int) -> str:
    """Live canonical progress line for the ``on_step(done, total)`` callback.

    The callback lands AFTER a step (and its advisory gate) completes: while
    later steps run we show the style-refinement line with the bar; the final
    callback means the drawing just passed its last check, right before the
    sheet stage takes over.
    """
    if done >= total:
        return _STAGE_TEXT["gate"]
    return f"{_STAGE_TEXT['style']} {_progress_bar(done, total)} {done}/{total}"


def _picked_count(data: dict[str, Any]) -> int:
    """Selected standard + custom count, uncapped (for the 15-limit guard)."""
    std = {i for i in data.get("std_sel", []) if 0 <= i < len(STANDARD_BLOCK)}
    return len(std) + len(data.get("custom", []))


async def _alpha_wallet(db: Database, user_id: int) -> dict[str, Any]:
    """FSM seed for money-aware screens: ``{"alpha": True, "bal": credits}``.

    Empty outside alpha and for admins, so the pure screen renderer can decide
    whether to show the price/balance line without a DB handle. Captured at
    flow entry and refreshed after every charge — packs change only through
    those moments, so staleness is bounded to an admin /gen mid-flow.
    """
    if _is_first_admin(user_id) or await modes.get_mode(db) != modes.ALPHA:
        return {}
    return {"alpha": True, "bal": await db.credits_left(user_id)}


def _money_line(data: dict[str, Any]) -> str | None:
    """«💸 спишет … · 💎 у тебя …» for alpha flows; None when not applicable."""
    if not data.get("alpha"):
        return None
    cost = pricing.cost_for_mode(data.get("mode", "fresh"))
    line = f"💸 Создание спишет {pricing.format_packs(cost)} пак"
    bal = data.get("bal")
    if isinstance(bal, int):
        line += f" · 💎 у тебя {pricing.format_packs(bal)}"
    return line + ". Ошибки бесплатны."


def _format_random_idea(item: str) -> str:
    """The 🎲 sub-screen text: the rolled idea + how to use or replace it."""
    return (
        f"🎲 Случайная идея:\n\n{item}\n\n"
        "Можно «Взять эту», крутануть ещё раз, скопировать и доработать — "
        "или просто пришлите свой текст сообщением."
    )


@contextlib.asynccontextmanager
async def _typing(message: Message) -> AsyncIterator[None]:
    """Keep an 'uploading photo…' chat action alive during a long task."""

    async def beat() -> None:
        while True:
            with contextlib.suppress(Exception):
                await message.bot.send_chat_action(message.chat.id, "upload_photo")  # type: ignore[union-attr]
            await asyncio.sleep(4)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _photo_check_timeout() -> asyncio.Timeout:
    """Cap the photo vision check so a stalled provider can't freeze the flow."""
    return asyncio.timeout(get_settings().photo_check_timeout_s)


def _generation_timeout() -> asyncio.Timeout:
    """Cap a long generation run so a hung model surfaces instead of hanging."""
    return asyncio.timeout(get_settings().generation_timeout_s)


class NewPack(StatesGroup):
    """FSM states for building a brand-new pack with a new character."""

    photo = State()
    name = State()
    subject = State()
    child_age = State()
    style = State()
    select_std = State()  # toggling standard captions (checklist)
    ask_custom = State()  # «добавить свои? да/нет»
    enter_custom = State()  # awaiting a custom caption (force reply)
    review = State()  # numbered list + create/add/remove
    publish = State()  # stickers generated, preview shown, awaiting publish/download


class Redraw(StatesGroup):
    """FSM for redrawing an existing character's canonical from a new photo."""

    photo = State()


# --- keyboards (pure) --------------------------------------------------------


def _attach_nav(kb: InlineKeyboardBuilder, *, back: bool) -> None:
    """Append a trailing ``⬅️ Назад`` / ``❌ Отмена`` row to a builder."""
    nav = InlineKeyboardBuilder()
    if back:
        nav.button(text="⬅️ Назад", callback_data="nav:back")
    nav.button(text="❌ Отмена", callback_data="nav:cancel")
    nav.adjust(2)
    kb.attach(nav)


def _nav_kb(*, back: bool) -> Any:
    """A keyboard with only the nav row (for text-input screens)."""
    kb = InlineKeyboardBuilder()
    _attach_nav(kb, back=back)
    return kb.as_markup()


def subject_kb() -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="Взрослый", callback_data="subject:adult")
    kb.button(text="Ребёнок", callback_data="subject:child")
    kb.adjust(2)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def age_kb() -> Any:
    kb = InlineKeyboardBuilder()
    for age in range(19):  # 0..18 (§5.3)
        kb.button(text=str(age), callback_data=f"age:{age}")
    kb.adjust(5)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def style_kb(loader: StyleLoader) -> Any:
    """Polished styles + a shelf button when experimental ones exist."""
    kb = InlineKeyboardBuilder()
    for style_id, display in loader.menu():
        kb.button(text=display, callback_data=f"style:{style_id}")
    if loader.has_experimental():
        kb.button(text="🧪 Экспериментальные", callback_data="styles:exp")
    kb.adjust(1)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def style_experimental_kb(loader: StyleLoader) -> Any:
    """The experimental shelf: experimental styles + a way back to the main ones."""
    kb = InlineKeyboardBuilder()
    for style_id, display in loader.menu(experimental=True):
        kb.button(text=display, callback_data=f"style:{style_id}")
    kb.button(text="⬅️ К основным стилям", callback_data="styles:main")
    kb.adjust(1)
    _attach_nav(kb, back=False)  # "back" here is the shelf button above; keep only Cancel
    return kb.as_markup()


def publish_kb(*, can_publish: bool = True) -> Any:
    kb = InlineKeyboardBuilder()
    if can_publish:
        kb.button(text="✅ Опубликовать в Telegram", callback_data="pub:yes")
    kb.button(text="⬇️ Скачать (zip)", callback_data="pub:dl")
    kb.button(text="❌ Отмена", callback_data="pub:no")
    kb.adjust(1)
    return kb.as_markup()


def std_checklist_kb(selected: list[int], page: int) -> Any:
    """Checklist of standard captions: toggles + page nav + done + back/cancel."""
    from sticker_service.services.stickers import PER_PAGE, STANDARD_BLOCK, prompt_idea

    kb = InlineKeyboardBuilder()
    pages = max(1, (len(STANDARD_BLOCK) + PER_PAGE - 1) // PER_PAGE)
    start = page * PER_PAGE
    page_items = list(enumerate(STANDARD_BLOCK))[start : start + PER_PAGE]
    for i, caption in page_items:
        mark = "✅" if i in selected else "⬜"
        # Полная прозрачность: на кнопке ровно та строка, что уйдёт в промпт.
        kb.button(text=f"{mark} {prompt_idea(caption)}", callback_data=f"std:{i}")
    kb.adjust(3)  # 3 columns → up to 3×4 per page
    # Bulk toggles: flip every standard caption at once (handy to clear, then
    # pick a few, or re-select all). Capped at the per-sheet limit.
    bulk = InlineKeyboardBuilder()
    bulk.button(text="✅ Отметить все", callback_data="stdall")
    bulk.button(text="🧹 Снять все", callback_data="stdclear")
    bulk.adjust(2)
    kb.attach(bulk)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀", callback_data=f"stdpage:{page - 1}")
    if page < pages - 1:
        nav.button(text="▶", callback_data=f"stdpage:{page + 1}")
    nav.button(text="Далее ▶▶", callback_data="stddone")
    kb.attach(nav)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def ask_custom_kb() -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data="cust:yes")
    kb.button(text="Нет", callback_data="cust:no")
    kb.adjust(2)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def enter_custom_kb() -> Any:
    """The idea-input screen: type your own, or roll a 🎲 random prompt."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Случайный промт", callback_data="randidea")
    kb.adjust(1)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def random_idea_kb(item: str) -> Any:
    """Actions under a rolled idea: take it, reroll, copy it into the input."""
    from aiogram.types import CopyTextButton

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Взять эту", callback_data="randtake")
    kb.button(text="🎲 Ещё", callback_data="randidea")
    # Copies the idea to the clipboard so it can be pasted into the input
    # field and edited before sending (closest Bot API gets to pre-filling).
    kb.button(text="📋 Скопировать", copy_text=CopyTextButton(text=item[:256]))
    kb.adjust(2, 1)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def review_kb(total: int) -> Any:
    from sticker_service.services.stickers import MAX_CAPTIONS

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Создать стикерпак", callback_data="rev:create")
    if total < MAX_CAPTIONS:
        kb.button(text="➕ Добавить стикер", callback_data="rev:add")
    if total > 0:
        kb.button(text="➖ Убрать стикер", callback_data="rev:remove")
    kb.adjust(1)
    _attach_nav(kb, back=True)
    return kb.as_markup()


def _review_text(captions: list[str]) -> str:
    """Numbered caption list for the review screen (+ limit notice when full)."""
    if not captions:
        return "Пока ничего не выбрано. Добавьте хотя бы один стикер."
    listing = "\n".join(f"{i}. {c}" for i, c in enumerate(captions, start=1))
    text = f"Стикеры ({len(captions)}):\n{listing}"
    if len(captions) >= MAX_CAPTIONS:
        text += (
            f"\n\n⚠️ Это максимум — {MAX_CAPTIONS} стикеров за один проход (один лист). "
            "Больше за раз не добавить; уберите лишний, чтобы освободить место."
        )
    # Expectations are set at the moment of decision (owner's rule, 12.06):
    # the full version lives on /start and /help.
    text += (
        "\n\n🎨 ИИ не идеален: образ и подписи могут выйти не такими, как "
        "задумано. Мы постоянно улучшаем качество; заметил косяк — /report."
    )
    return text


# --- screen rendering + back navigation (pure) -------------------------------


def _screen_for(
    target: str | None, data: dict[str, Any], loader: StyleLoader | None
) -> tuple[str, Any]:
    """Render a wizard step to ``(text, keyboard)`` — the single source of truth.

    Forward transitions and the Back button both call this so a step looks
    identical no matter how it was reached. ``loader`` is required only for the
    style step.
    """
    if target == NewPack.name.state:
        return _TXT_NAME, _nav_kb(back=False)
    if target == NewPack.subject.state:
        return _TXT_SUBJECT, subject_kb()
    if target == NewPack.child_age.state:
        return _TXT_AGE, age_kb()
    if target == NewPack.style.state:
        if loader is None:
            raise ValueError("style screen needs a StyleLoader")
        return _TXT_STYLE, style_kb(loader)
    if target == NewPack.select_std.state:
        selected = list(data.get("std_sel", []))
        page = int(data.get("page", 0))
        text = _TXT_CAPTIONS
        if (money := _money_line(data)) is not None:
            text += f"\n\n{money}"
        return text, std_checklist_kb(selected, page)
    if target == NewPack.ask_custom.state:
        return _TXT_ASK_CUSTOM, ask_custom_kb()
    if target == NewPack.enter_custom.state:
        return _TXT_ENTER_CUSTOM, enter_custom_kb()
    if target == NewPack.review.state:
        captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
        text = _review_text(captions)
        if (money := _money_line(data)) is not None:
            text += f"\n\n{money}"
        return text, review_kb(len(captions))
    raise ValueError(f"no screen for state {target!r}")  # pragma: no cover - guard


def _prev_state(current: str | None, data: dict[str, Any]) -> str | None:
    """Map the current wizard step to the one ``⬅️ Назад`` should return to."""
    if current == NewPack.subject.state:
        return NewPack.name.state
    if current == NewPack.child_age.state:
        return NewPack.subject.state
    if current == NewPack.style.state:
        return NewPack.child_age.state if data.get("subject") == "child" else NewPack.subject.state
    if current == NewPack.select_std.state:
        return NewPack.style.state
    if current == NewPack.ask_custom.state:
        return NewPack.select_std.state
    if current == NewPack.enter_custom.state:
        back = data.get("custom_back")
        return back if isinstance(back, str) else NewPack.ask_custom.state
    if current == NewPack.review.state:
        return NewPack.ask_custom.state
    return None


# --- wizard-message plumbing (I/O) -------------------------------------------


async def _store_wizard(state: FSMContext, msg: Message) -> None:  # pragma: no cover
    """Remember which message is the editable wizard for this flow."""
    await state.update_data(wizard_chat_id=msg.chat.id, wizard_msg_id=msg.message_id)


async def _show(  # pragma: no cover
    callback: CallbackQuery,
    state: FSMContext,
    target: str | None,
    loader: StyleLoader | None,
    data: dict[str, Any] | None = None,
) -> None:
    """Set ``target`` and edit the wizard message in place (callback-driven)."""
    data = data if data is not None else await state.get_data()
    text, markup = _screen_for(target, data, loader)
    await state.set_state(target)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
            await _store_wizard(state, callback.message)
            return
        except TelegramBadRequest:
            # Wizard message gone (deleted/too old) — don't silently no-op; send
            # a fresh one so navigation never looks frozen.
            pass
        sent = await callback.message.answer(text, reply_markup=markup)
        await _store_wizard(state, sent)


async def _show_msg(  # pragma: no cover
    message: Message,
    state: FSMContext,
    target: str | None,
    loader: StyleLoader | None,
    data: dict[str, Any] | None = None,
) -> None:
    """Set ``target`` and edit the stored wizard message (message-driven)."""
    data = data if data is not None else await state.get_data()
    text, markup = _screen_for(target, data, loader)
    await state.set_state(target)
    cid = data.get("wizard_chat_id")
    mid = data.get("wizard_msg_id")
    if cid is not None and mid is not None:
        try:
            await message.bot.edit_message_text(  # type: ignore[union-attr]
                text, chat_id=cid, message_id=mid, reply_markup=markup
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(text, reply_markup=markup)
    await _store_wizard(state, sent)


async def _replace_below(  # pragma: no cover
    message: Message, state: FSMContext, text: str, markup: Any = None
) -> Message:
    """Drop the current wizard message and send a fresh one *below* the user's
    media, so the bot's controls sit under the photo rather than above it."""
    data = await state.get_data()
    cid = data.get("wizard_chat_id")
    mid = data.get("wizard_msg_id")
    if cid is not None and mid is not None:
        with contextlib.suppress(TelegramBadRequest):
            await message.bot.delete_message(cid, mid)  # type: ignore[union-attr]
    sent = await message.answer(text, reply_markup=markup)
    await _store_wizard(state, sent)
    return sent


# --- handlers ----------------------------------------------------------------


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_set


def _is_first_admin(user_id: int) -> bool:
    """The owner — the only account with unlimited, unbilled generations.

    Other admins keep admin powers but generate like ordinary alpha testers
    (budget-gated, charged the standard pack credits).
    """
    return user_id == get_settings().first_admin_id


async def _alpha_gate(db: Database, user_id: int) -> str | None:  # pragma: no cover
    """In alpha, non-approved users must apply first."""
    if _is_admin(user_id) or await modes.get_mode(db) != modes.ALPHA:
        return None
    if not await db.is_allowed(user_id):
        return "🔒 Бот в альфа-тесте. Оставьте заявку через /start — мы пригласим вас."
    return None


async def _generation_gate(db: Database, user_id: int, cost: int) -> str | None:  # pragma: no cover
    """Budget + per-user credit check for an action costing ``cost`` credits."""
    if _is_first_admin(user_id) or await modes.get_mode(db) != modes.ALPHA:
        return None
    if not await budget.enough_for(db, 2):
        return (
            "⛔ Тестирование временно приостановлено из-за исчерпания бюджета. "
            "Скоро либо пополним бюджет, либо перейдём в бета-стадию — ждите уведомлений."
        )
    left = await db.credits_left(user_id)
    if left < cost:
        return (
            f"Недостаточно паков: действие стоит {pricing.format_packs(cost)}, "
            f"а осталось {pricing.format_packs(left)}. "
            "Огромное спасибо за участие в альфе проекта! 🙏"
        )
    return None


async def cmd_new(
    message: Message, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:
    """Start a new pack: record implicit photo-rights consent, then ask for a photo.

    Sending a photo to the bot is itself the consent act (§15.2): we record the
    fact + timestamp here instead of nagging with a separate confirmation step.
    """
    tag_component("handlers.flow")
    uid = message.from_user.id if message.from_user else 0
    if (hint := await _alpha_gate(db, uid)) is not None:
        await message.answer(hint)
        return
    await _drop_review_scratch(orchestrator, state)
    await state.clear()
    if uid:
        await db.record_consent(uid)
    await state.set_state(NewPack.photo)
    await state.update_data(**await _alpha_wallet(db, uid))
    sent = await message.answer(_TXT_PHOTO, reply_markup=_nav_kb(back=False))
    await _store_wizard(state, sent)


_PHOTO_HINTS = {
    photo_check.NO_PERSON: "Не вижу человека на фото. Пришли фото, где есть человек.",
    photo_check.MULTI: "На фото больше одного человека. Нужен ровно один.",
    photo_check.SMALL: "Человек слишком мелкий — пусть занимает хотя бы 1/5 кадра.",
}


async def _download_photo(message: Message, status: Message) -> bytes | None:  # pragma: no cover
    """Download the largest photo; on a Telegram failure, tell the user and return None.

    Without this, a transient ``bot.download`` error escaped the handler and left
    the wizard frozen on "проверяю фото…" forever.
    """
    try:
        file = await message.bot.download(message.photo[-1].file_id)  # type: ignore[union-attr]
    except Exception as exc:
        logger.warning("photo download failed: %s", str(exc)[:100])
        with contextlib.suppress(Exception):
            await status.edit_text("Не удалось скачать фото из Telegram — пришли его ещё раз.")
        return None
    return file.read() if file else b""


async def on_photo(  # pragma: no cover
    message: Message, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Accept + validate the photo (vision foolproof check), then ask for a name.

    The photo is kept in the chat; the wizard is re-sent *below* it so the next
    prompt sits under the image.
    """
    tag_component("handlers.flow")
    if not message.photo:
        await message.answer("Нужно именно фото. Пришли изображение человека.")
        return
    await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
    status = await _replace_below(message, state, "📸 Принял фото, проверяю…")
    photo = await _download_photo(message, status)
    if photo is None:
        return  # download failed → user was told to resend; stay in photo state
    uid = message.from_user.id if message.from_user else 0
    try:
        async with _photo_check_timeout():
            code = await orchestrator.validate_photo(photo)
    except Exception as exc:  # vision check failed/stalled — let it through rather than block
        logger.warning("photo check failed: %s", str(exc)[:100])
        code = None
    if code == photo_check.NUDE:
        with contextlib.suppress(TelegramBadRequest):
            await status.delete()
        await _strike(db, uid, message, "На фото обнажёнка")
        return
    if code is not None:  # stay in photo state; the prompt sits below the bad photo
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text(_PHOTO_HINTS.get(code, "Фото не подходит, пришли другое."))
        return
    await state.update_data(photo=photo)
    text, markup = _screen_for(NewPack.name.state, await state.get_data(), None)
    await state.set_state(NewPack.name)
    with contextlib.suppress(TelegramBadRequest):
        await status.edit_text(text, reply_markup=markup)
    await _store_wizard(state, status)


async def _strike(
    db: Database, user_id: int, msg: Message, reason: str
) -> None:  # pragma: no cover
    """Record a moderation strike and tell the user (ban enforced by middleware)."""
    count, until = await register_strike(db, user_id, reason)
    text = f"⚠️ {reason}. Это нарушение правил (/rules). Страйков: {count}."
    if until is not None:
        text += " Вы временно заблокированы."
    await msg.answer(text)


async def _alert_admins_quota(msg: Message, exc: Exception) -> None:  # pragma: no cover
    """DM every admin once a generation fails because the model is out of credits."""
    text = f"🔴 Генерация остановлена: у провайдера закончились кредиты.\n{str(exc)[:200]}"
    for admin_id in get_settings().admin_id_list:
        with contextlib.suppress(Exception):
            await msg.bot.send_message(admin_id, text)  # type: ignore[union-attr]


async def _alert_owner_genfail(msg: Message, user_id: int, what: str, exc: Exception) -> None:
    """DM the owner whenever ANYTHING fails for a tester (gen/publish/redraw).

    The user sees only a friendly text (raw internals are hidden by policy),
    so the raw reason must reach the owner instantly — not sit unseen in
    Sentry's grouping delay. ``what`` names the failed action in Russian.
    A rejected-sheet artifact attached to the exception is sent even for the
    owner's own failures: the sheet is evidence nobody can see otherwise.
    """
    owner = get_settings().first_admin_id
    if owner is None:
        return
    rejected = getattr(exc, "rejected_path", None)
    if rejected is not None:
        with contextlib.suppress(Exception):
            await msg.bot.send_document(  # type: ignore[union-attr]
                owner,
                FSInputFile(rejected),
                caption=f"🗑 Отбракованный лист (тестер id={user_id}): {str(exc)[:200]}",
            )
    if user_id == owner:  # the owner sees his own failures live
        return
    reason = str(exc)[:150] or type(exc).__name__
    with contextlib.suppress(Exception):
        await msg.bot.send_message(  # type: ignore[union-attr]
            owner,
            f"⚠️ У тестера id={user_id} сбой: {what}.\n"
            f"Причина: {reason}\n"
            f'Профиль: <a href="tg://user?id={user_id}">открыть</a>',
            parse_mode="HTML",
        )


async def on_name(message: Message, state: FSMContext, db: Database) -> None:
    """Store the human name (moderated), drop the typed message, then ask adult/child."""
    tag_component("handlers.flow")
    name = (message.text or "").strip()
    reason = caption_rejection_reason(name)
    if reason:
        await _strike(
            db,
            message.from_user.id if message.from_user else 0,
            message,
            f"Так назвать нельзя ({reason})",
        )
        return
    await state.update_data(name=name or "Мой пак")
    with contextlib.suppress(TelegramBadRequest):
        await message.delete()
    await _show_msg(message, state, NewPack.subject.state, None)


async def on_subject(callback: CallbackQuery, state: FSMContext, loader: StyleLoader) -> None:
    """Branch on subject: ask age ONLY for a child (§B.4)."""
    tag_component("handlers.flow")
    subject = (callback.data or "").split(":", 1)[-1]
    await callback.answer()
    if subject == "child":
        await state.update_data(subject="child")
        await _show(callback, state, NewPack.child_age.state, loader)
    else:
        await state.update_data(subject="adult", child_age=None)
        await _show(callback, state, NewPack.style.state, loader)


async def on_age(callback: CallbackQuery, state: FSMContext, loader: StyleLoader) -> None:
    """Store the child's age (0..18), then ask for a style."""
    tag_component("handlers.flow")
    age = int((callback.data or "age:0").split(":", 1)[-1])
    await callback.answer()
    await state.update_data(child_age=age)
    await _show(callback, state, NewPack.style.state, loader)


# --- caption selection → create → preview → publish/download -----------------


async def _enter_captions(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    orchestrator: Orchestrator,
    *,
    mode: str,
    **extra: Any,
) -> None:
    """Seed caption state (all standard selected) and show the checklist.

    The meme pool plays through the 🎲 «Случайный промт» button on the
    idea-input step instead of pre-filled toggles, so the default here is the
    plain full standard block again.

    ``reuse``/``extend`` jump straight here without a photo/name/style collection
    phase, so any leftover data from a previous *unfinished* ``/new`` flow still
    sits in the persistent FSM. Reset it first, or that stale ``mode``/``name``/
    ``photo`` can hijack publishing into a wrong NEW pack instead of reusing /
    extending the chosen one. ``fresh`` keeps the data it just collected.
    """
    if mode != "fresh":
        # The wipe below would orphan a previous review's scratch dir — drop it.
        await _drop_review_scratch(orchestrator, state)
        await state.set_data({})
    user_id = callback.from_user.id if callback.from_user else 0
    await state.update_data(
        mode=mode,
        std_sel=list(range(len(STANDARD_BLOCK))),
        custom=[],
        page=0,
        **await _alpha_wallet(db, user_id),
        **extra,
    )
    await callback.answer()
    await _show(callback, state, NewPack.select_std.state, None)


async def on_style(
    callback: CallbackQuery, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:  # pragma: no cover
    """Store the style and start caption selection (canonical is built on Create)."""
    tag_component("handlers.flow")
    style_id = (callback.data or "").split(":", 1)[-1]
    if callback.from_user is not None:
        await analytics.log(db, callback.from_user.id, analytics.STYLE_CHOSEN, style_id=style_id)
    await _enter_captions(callback, state, db, orchestrator, mode="fresh", style_id=style_id)


async def on_styles_exp(callback: CallbackQuery, loader: StyleLoader) -> None:  # pragma: no cover
    """Open the experimental shelf in place — still the ``style`` step, just a sub-view.

    Selecting a style there fires the same ``style:`` callback and walks the
    normal workflow; ``styles:main`` returns to the polished list.
    """
    tag_component("handlers.flow")
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(
                _TXT_STYLE_EXP, reply_markup=style_experimental_kb(loader)
            )


async def on_styles_main(callback: CallbackQuery, loader: StyleLoader) -> None:  # pragma: no cover
    """Return from the experimental shelf to the polished styles, in place."""
    tag_component("handlers.flow")
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(_TXT_STYLE, reply_markup=style_kb(loader))


_TXT_CAP_FULL = f"Больше {MAX_CAPTIONS} нельзя — сначала снимите какую-нибудь галочку."


async def on_std_toggle(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    idx = int((callback.data or "std:0").split(":", 1)[-1])
    data = await state.get_data()
    selected = list(data.get("std_sel", []))
    if idx in selected:
        selected.remove(idx)
    else:
        if _picked_count(data) >= MAX_CAPTIONS:
            await callback.answer(_TXT_CAP_FULL)  # toast, no popup, no message
            return
        selected.append(idx)
    await state.update_data(std_sel=selected)
    page = int(data.get("page", 0))
    with contextlib.suppress(Exception):
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=std_checklist_kb(selected, page))
    await callback.answer()


async def on_std_bulk(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Select-all / clear-all for the standard caption checklist.

    Select-all fills only the room left by the user's customs, so the overall
    selection never exceeds the per-sheet cap.
    """
    tag_component("handlers.flow")
    select_all = (callback.data or "") == "stdall"
    data = await state.get_data()
    room = max(0, MAX_CAPTIONS - len(data.get("custom", [])))
    selected = list(range(len(STANDARD_BLOCK)))[:room] if select_all else []
    await state.update_data(std_sel=selected)
    page = int(data.get("page", 0))
    with contextlib.suppress(Exception):
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=std_checklist_kb(selected, page))
    if select_all and len(selected) < len(STANDARD_BLOCK):
        await callback.answer(f"Отметил {len(selected)} — всего можно {MAX_CAPTIONS}.")
    else:
        await callback.answer("Отмечены все" if select_all else "Галки сняты")


async def on_std_page(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    page = int((callback.data or "stdpage:0").split(":", 1)[-1])
    await state.update_data(page=page)
    data = await state.get_data()
    with contextlib.suppress(Exception):
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(
                reply_markup=std_checklist_kb(list(data.get("std_sel", [])), page)
            )
    await callback.answer()


async def on_std_done(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    await _show(callback, state, NewPack.ask_custom.state, None)
    await callback.answer()


async def on_custom_yes(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    await state.update_data(custom_back=NewPack.ask_custom.state)
    await _show(callback, state, NewPack.enter_custom.state, None)
    await callback.answer()


def _is_duplicate_caption(data: dict[str, Any], text: str) -> bool:
    """True when an equal caption is already in the order (standard or custom).

    Standard buttons are toggle-safe by design; this guards the custom paths
    (typed text and the 🎲 dice), where a duplicate idea line in the prompt
    makes the model honestly draw the same sticker twice.
    """
    norm = text.casefold().strip()
    picked = [STANDARD_BLOCK[i] for i in data.get("std_sel", []) if 0 <= i < len(STANDARD_BLOCK)]
    picked += list(data.get("custom", []))
    return any(norm == c.casefold().strip() for c in picked)


async def on_random_idea(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    """🎲 «Случайный промт»: roll an idea from the meme pool onto the input screen.

    The rolled text is shown with «Взять эту» / «Ещё» / copy-to-clipboard (the
    closest Bot API gets to pre-filling the user's input field); typing a own
    message still works — the FSM state stays on the input step.
    """
    tag_component("handlers.flow")
    from sticker_service.services.stickers import active_pool

    try:
        pool = await active_pool(db)
    except Exception:  # the dice must never break the input step
        pool = []
    if not pool:
        await callback.answer("Идеи сейчас недоступны — напишите свою.")
        return
    item = random.choice(pool).as_sheet_item()  # nosec B311 - variety, not crypto
    await state.update_data(rand_idea=item)
    with contextlib.suppress(Exception):
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                _format_random_idea(item), reply_markup=random_idea_kb(item)
            )
    await callback.answer()


async def on_random_take(callback: CallbackQuery, state: FSMContext) -> None:
    """➕ «Взять эту»: append the rolled idea as a custom item and show the review."""
    tag_component("handlers.flow")
    data = await state.get_data()
    item = data.get("rand_idea")
    if not item:
        await callback.answer("Сначала крутаните 🎲")
        return
    if _picked_count(data) >= MAX_CAPTIONS:
        await callback.answer(_TXT_CAP_FULL)
        return
    if _is_duplicate_caption(data, item):
        # The dice can roll the same pool item twice — a duplicate idea line
        # makes the model honestly draw the same sticker twice.
        await state.update_data(rand_idea=None)
        await callback.answer("Такая идея уже есть в наборе 😉")
        return
    custom = [*list(data.get("custom", [])), item]
    await state.update_data(custom=custom, rand_idea=None)
    await callback.answer("Добавил 🎲")
    await _show(callback, state, NewPack.review.state, None)


async def on_custom_no(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    await _show(callback, state, NewPack.review.state, None)
    await callback.answer()


async def on_enter_custom(
    message: Message, state: FSMContext, db: Database
) -> None:  # pragma: no cover
    """Append a typed custom caption (capped at MAX_CAPTIONS), then show the review list."""
    tag_component("handlers.flow")
    text = (message.text or "").strip()
    reason = caption_rejection_reason(text)
    if reason:
        await _strike(
            db, message.from_user.id if message.from_user else 0, message, f"Так нельзя ({reason})"
        )
        return
    data = await state.get_data()
    if text and _is_duplicate_caption(data, text):
        await message.answer("Такая подпись уже есть в наборе — придумай другую 😉")
        return
    custom = list(data.get("custom", []))
    if text and _picked_count(data) < MAX_CAPTIONS:
        custom.append(text)
        await state.update_data(custom=custom)
    with contextlib.suppress(TelegramBadRequest):
        await message.delete()
    await _show_msg(message, state, NewPack.review.state, None)


async def on_rev_add(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    await state.update_data(custom_back=NewPack.review.state)
    await _show(callback, state, NewPack.enter_custom.state, None)
    await callback.answer()


async def on_rev_remove(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    data = await state.get_data()
    captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(captions):
        kb.button(text=f"➖ {c}", callback_data=f"rem:{i}")
    kb.adjust(2)
    back = InlineKeyboardBuilder()
    back.button(text="⬅️ Назад", callback_data="rev:show")
    kb.attach(back)
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text("Какой убрать?", reply_markup=kb.as_markup())


async def on_rev_show(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Return to the review list (from the remove sub-screen)."""
    tag_component("handlers.flow")
    await _show(callback, state, NewPack.review.state, None)
    await callback.answer()


async def on_rem_pick(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Remove one item by its review position: standard first, then customs."""
    tag_component("handlers.flow")
    pos = int((callback.data or "rem:0").split(":", 1)[-1])
    data = await state.get_data()
    std_sel = sorted(set(data.get("std_sel", [])))
    custom = list(data.get("custom", []))
    if pos < len(std_sel):
        std_sel.remove(std_sel[pos])
    elif pos - len(std_sel) < len(custom):
        custom.pop(pos - len(std_sel))
    await state.update_data(std_sel=std_sel, custom=custom)
    await callback.answer("Убрал")
    await _show(callback, state, NewPack.review.state, None)


#: Forward-wizard callback prefixes; a tap that no state-filtered handler took
#: is a button on a DEAD message (the pack finished or was cancelled) — answer
#: kindly instead of walking the wizard into a crash with missing data.
_WIZARD_PREFIXES = (
    "subject:",
    "age:",
    "style:",
    "styles:",
    "std",
    "cust:",
    "randidea",
    "randtake",
    "rev:",
    "rem:",
    "retry:",
    "pub:",
)


def _is_wizard_callback(data: str | None) -> bool:
    return bool(data) and data.startswith(_WIZARD_PREFIXES)


async def on_stale_wizard(callback: CallbackQuery) -> None:  # pragma: no cover
    """A wizard button from a finished/cancelled pack — explain, don't crash."""
    tag_component("handlers.flow")
    with contextlib.suppress(Exception):
        await callback.answer(
            "Эта кнопка уже устарела — тот пак закрыт. Начни новый: /new", show_alert=True
        )
    # Strip the dead keyboard so the message can't mislead twice.
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)


async def on_nav_back(callback: CallbackQuery, state: FSMContext, loader: StyleLoader) -> None:
    """``⬅️ Назад`` — re-render the previous wizard step in place."""
    tag_component("handlers.flow")
    data = await state.get_data()
    target = _prev_state(await state.get_state(), data)
    await callback.answer()
    if target is not None:
        await _show(callback, state, target, loader, data)


async def on_nav_cancel(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator
) -> None:
    """``❌ Отмена`` — abort the flow and collapse the wizard message."""
    tag_component("handlers.flow")
    await _drop_review_scratch(orchestrator, state)
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(f"Отменено. {_TXT_NEW_OR_ADDTO}")


# --- single-flight guard: one paid/publish action per user at a time ----------
# aiogram runs handlers as concurrent coroutines on one event loop, so a plain
# set is a safe mutex (no await between the check and the add). This stops a
# double-tap from starting two generations (double spend) or two publishes
# (a duplicate Telegram pack).
_inflight_users: set[int] = set()
_BUSY_TEXT = "⏳ Уже выполняется — дождись результата."


def _begin_action(user_id: int) -> bool:
    """Reserve the user's single in-flight slot; False if one is already running."""
    if user_id in _inflight_users:
        return False
    _inflight_users.add(user_id)
    return True


def _end_action(user_id: int) -> None:
    _inflight_users.discard(user_id)


async def on_rev_create(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Generate the selected stickers (per page) and show a preview to publish/download."""
    tag_component("handlers.flow")
    user_id = callback.from_user.id if callback.from_user else 0
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    spawned = False
    try:
        data = await state.get_data()
        captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        if not captions:
            await msg.answer("Выберите хотя бы один стикер.")
            return
        mode = data.get("mode", "fresh")
        cost = pricing.cost_for_mode(mode)
        if (hint := await _generation_gate(db, user_id, cost)) is not None:
            await msg.answer(hint)
            return

        # The price/balance line already sits on the selection and review
        # screens (_money_line), so no extra "спишет…" message here.
        std_names = [STANDARD_BLOCK[i] for i in sorted(set(data.get("std_sel", [])))]
        await analytics.log(
            db,
            user_id,
            analytics.CAPTIONS_SELECTED,
            standard=std_names,
            custom=list(data.get("custom", [])),
            total=len(captions),
        )
        _spawn_generation(msg, state, orchestrator, db, user_id)
        spawned = True
    finally:
        if not spawned:
            _end_action(user_id)


# --- generation core + retry-on-overload (shared by create & retry) ----------

_RETRY_DELAY_S = 20
_bg_tasks: set[asyncio.Task[None]] = set()


def _track_bg(task: asyncio.Task[None]) -> None:
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


class StatusLine:
    """Live wizard status that always says what is happening RIGHT NOW.

    Stage lines stick; a retry/fallback notice is a MOMENT, not a state — it
    shows for a few seconds and reverts to the current stage; a heartbeat
    appends elapsed time when a stage hangs for a while, so a long model
    call never looks frozen (a frozen line reads as "broken" to the user).
    A stage given as a tuple of frames — or a string ending in «…», which
    auto-expands to dot frames — is animated: frames rotate every
    FRAME_SECONDS (the elapsed timer joins once the stage hangs past
    HEARTBEAT_SECONDS), pausing while a notice is on screen so the notice
    stays readable.
    """

    NOTICE_SECONDS = 5.0
    HEARTBEAT_SECONDS = 20.0
    FRAME_SECONDS = 2.0

    def __init__(self, msg: Message) -> None:
        self._msg = msg
        self._stage = ""
        self._frames: tuple[str, ...] = ()
        self._frame_no = 0
        self._seq = 0
        self._started = time.monotonic()
        self._changed = time.monotonic()
        self._notice_until = 0.0
        self._heartbeat: asyncio.Task[None] | None = None

    async def _edit(self, text: str) -> None:
        with contextlib.suppress(Exception):
            await self._msg.edit_text(text)

    async def stage(self, text: str | tuple[str, ...]) -> None:
        """Show the current stage; frames (or a trailing «…») rotate until the next one."""
        self._seq += 1
        frames = _dot_frames(text) if isinstance(text, str) else text
        self._frames = frames
        self._frame_no = 0
        self._stage = frames[0]
        self._changed = time.monotonic()
        self._notice_until = 0.0
        await self._edit(frames[0])

    async def notice(self, text: str) -> None:
        """Show a transient event, then fall back to the stage line."""
        self._seq += 1
        seq = self._seq
        self._changed = time.monotonic()
        self._notice_until = time.monotonic() + self.NOTICE_SECONDS
        await self._edit(text)

        async def _revert() -> None:
            await asyncio.sleep(self.NOTICE_SECONDS)
            if self._seq == seq and self._stage:  # nothing newer was shown
                self._changed = time.monotonic()
                await self._edit(self._stage)

        _track_bg(asyncio.create_task(_revert()))

    def start(self) -> None:
        """Begin the liveness ticker (call ``stop`` when generation ends)."""

        async def _beat() -> None:
            last_beat = time.monotonic()
            while True:
                await asyncio.sleep(min(self.FRAME_SECONDS, self.HEARTBEAT_SECONDS))
                now = time.monotonic()
                if not self._stage or now < self._notice_until:
                    continue
                elapsed = int(now - self._started)
                if len(self._frames) > 1:
                    self._frame_no = (self._frame_no + 1) % len(self._frames)
                    self._stage = self._frames[self._frame_no]
                    line = self._stage
                    # Dots show liveness by themselves; the timer is for real
                    # hangs, so it joins only once the stage overstays.
                    if now - self._changed >= self.HEARTBEAT_SECONDS:
                        line = f"{self._stage} · уже {elapsed} с"
                    await self._edit(line)
                    last_beat = now
                elif (
                    now - self._changed >= self.HEARTBEAT_SECONDS
                    and now - last_beat >= self.HEARTBEAT_SECONDS
                ):
                    await self._edit(f"{self._stage} · уже {elapsed} с")
                    last_beat = now

        self._heartbeat = asyncio.create_task(_beat())
        _track_bg(self._heartbeat)

    def stop(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()


async def revive_orphaned_generations(bot: Any, storage: Any) -> int:
    """Find flows a hard restart orphaned mid-generation and offer a retry.

    The drain keeps soft redeploys safe, but a crash/OOM/SIGKILL still leaves
    users in the ``publish`` state with a frozen status message and no worker.
    On boot we move them back to review and hand them the retry button — the
    persisted canonical steps make the retry resume, not restart. Returns how
    many users were revived; owner notification happens via the failure alert
    path only for real exceptions, so this also DMs the owner per orphan.
    """
    from aiogram.fsm.storage.base import StorageKey

    orphans = await storage.keys_in_state(NewPack.publish.state)
    # Only flows touched RECENTLY were truly interrupted; a row stuck for days
    # (user saw the preview and walked away) is abandoned — clear it silently,
    # or every deploy "revives" the same mummy and messages people at 1 AM
    # about a generation they never started (live: Zoya's June-8 row).
    fresh = set(await storage.keys_in_state(NewPack.publish.state, max_age_seconds=1800))
    owner = get_settings().first_admin_id
    for bot_id, chat_id, user_id in orphans:
        key = StorageKey(bot_id=bot_id, chat_id=chat_id, user_id=user_id)
        if (bot_id, chat_id, user_id) not in fresh:
            with contextlib.suppress(Exception):
                await storage.set_state(key, None)
            continue
        with contextlib.suppress(Exception):
            await storage.set_state(key, NewPack.review.state)
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id,
                "⚠️ Обновление бота прервало генерацию — прости! Нажми кнопку, "
                "и я продолжу с того места, где остановилась.",
                reply_markup=_retry_kb(0),
            )
        if owner is not None and user_id != owner:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    owner,
                    f"♻️ Рестарт прервал генерацию у тестера id={user_id} — "
                    "отправил ему кнопку «повторить».",
                )
    revived = len([o for o in orphans if o in fresh])
    if revived:
        logger.warning("revived %d generation(s) orphaned by a restart", revived)
    return revived


async def drain_generations(timeout: float) -> int:
    """Await in-flight background generations; return how many missed the deadline.

    Generation runs as detached tasks (freeing the bounded polling slots), so
    aiogram's own shutdown does NOT wait for them — without this drain a redeploy
    kills a user's generation mid-step. The bot calls this after polling stops
    and BEFORE closing sessions (the tasks still need Telegram/DB to finish),
    keeping the soft-redeploy promise within compose's ``stop_grace_period``.
    """
    tasks = {task for task in _bg_tasks if not task.done()}
    if not tasks:
        return 0
    logger.info("drain: waiting up to %.0fs for %d in-flight task(s)", timeout, len(tasks))
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        logger.warning("drain: %d task(s) still running at the deadline", len(pending))
    return len(pending)


def _spawn_generation(  # pragma: no cover
    msg: Message, state: FSMContext, orchestrator: Orchestrator, db: Database, user_id: int
) -> None:
    """Run generation as a tracked background task, freeing the dispatcher slot.

    Generation waits on Gemini for minutes; holding one of the bounded polling
    task slots (APP_POLLING_TASKS_LIMIT) that long would starve all update
    processing. The user's single-flight slot stays held by the task and is
    released in its ``finally`` — double-taps remain blocked for the duration.
    """

    async def _run() -> None:
        try:
            await _generate_and_present(msg, state, orchestrator, db, user_id)
        except Exception:
            # _generate_and_present reports its own failures to the user; this
            # catches the unexpected (e.g. a Telegram edit dying) so the task
            # never vanishes silently.
            logger.exception("background generation task failed")
        finally:
            _end_action(user_id)

    task = asyncio.create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _retry_kb(seconds_left: int) -> Any:
    """Retry control: a live countdown while the model cools down, then an active button."""
    kb = InlineKeyboardBuilder()
    if seconds_left > 0:
        kb.button(text=f"⏳ Попробовать ещё раз ({seconds_left})", callback_data="retry:wait")
    else:
        kb.button(text="🔄 Попробовать ещё раз", callback_data="retry:gen")
    return kb.as_markup()


def _arm_retry(msg: Message, *, delay: int = _RETRY_DELAY_S) -> None:  # pragma: no cover
    """Attach a retry button that counts down ``delay`` s (inactive), then activates."""

    async def _countdown() -> None:
        for left in range(delay, 0, -1):
            with contextlib.suppress(Exception):
                await msg.edit_reply_markup(reply_markup=_retry_kb(left))
            await asyncio.sleep(1)
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=_retry_kb(0))

    task = asyncio.create_task(_countdown())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def _generate_and_present(  # pragma: no cover
    msg: Message,
    state: FSMContext,
    orchestrator: Orchestrator,
    db: Database,
    user_id: int,
) -> None:
    """Run generation from the saved flow state and show the preview.

    Shared by the initial '✅ Создать' press and the '🔄 Попробовать ещё раз'
    retry, so a transient overload can be retried with the exact same inputs
    (which persist in the FSM state).
    """
    data = await state.get_data()
    captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
    if not captions:
        return
    mode = data.get("mode", "fresh")
    if mode == "fresh" and not (data.get("photo") and data.get("style_id") and data.get("subject")):
        # A stale path slipped through (old button, swept FSM): the pack's
        # session is gone — say so kindly, never leak the validation internals.
        with contextlib.suppress(Exception):
            await msg.edit_text("Этот пак уже закрыт. Начни новый: /new")
        return
    cost = pricing.cost_for_mode(mode)
    started = time.monotonic()
    await state.set_state(NewPack.publish)

    status = StatusLine(msg)

    async def on_step(done: int, total: int) -> None:
        await status.stage(_canonical_progress_text(done, total))

    async def on_stage(label: str) -> None:
        if label == "sheet":  # animated: frames rotate while the model draws
            await status.stage(_SHEET_FRAMES)
            return
        await status.stage(_STAGE_TEXT.get(label, "Работаю…"))

    async def on_notice(key: str) -> None:
        # A model retry/fallback is a MOMENT, not a state: StatusLine shows it
        # briefly and reverts to the live stage, so the line always reflects
        # what is happening right now and never looks frozen.
        await status.notice(_NOTICE_TEXT.get(key, "Работаю…"))

    # Fresh starts by drawing the photo; reuse/extend jump straight to the sheet.
    start_text = _STAGE_TEXT["photo_to_art"] if mode == "fresh" else "⚙️ Готовлю генерацию…"
    await status.stage(start_text)
    status.start()
    try:
        with notice_sink(on_notice):
            async with _typing(msg), _generation_timeout():
                bundle = await orchestrator.build_for_review(
                    mode=mode,
                    owner_id=user_id,
                    captions=captions,
                    on_step=on_step,
                    on_stage=on_stage,
                    photo=data.get("photo"),
                    style_id=data.get("style_id"),
                    subject_type=data.get("subject"),
                    child_age=data.get("child_age"),
                    name=data.get("name"),
                    character_id=data.get("character_id"),
                    pack_id=data.get("pack_id"),
                )
    except Exception as exc:
        logger.exception("generation failed")
        # Revert out of the publish state (set before generation) so a failure is
        # never a dead-end recoverable only via /cancel — the review screen's
        # "✅ Создать" works again, alongside any retry button.
        with contextlib.suppress(Exception):
            await state.set_state(NewPack.review)
        await analytics.log(
            db, user_id, analytics.GENERATION_ERROR, mode=mode, reason=str(exc)[:200]
        )
        with contextlib.suppress(Exception):
            await msg.edit_text(_friendly_error(exc))
        await _alert_owner_genfail(msg, user_id, f"генерация (режим: {mode})", exc)
        if model_errors.is_quota(exc):
            await _alert_admins_quota(msg, exc)
        if model_errors.is_retryable(exc):  # overload/timeout → offer a retry once it cools down
            _arm_retry(msg)
        return
    finally:
        status.stop()  # the heartbeat must never outlive the generation

    await analytics.log(
        db,
        user_id,
        analytics.GENERATION_DONE,
        mode=mode,
        count=len(bundle.stickers),
        seconds=round(time.monotonic() - started, 1),
    )
    await _present_for_publish(
        msg,
        state,
        bundle.stickers,
        mode=mode,
        title=bundle.title,
        pack_id=bundle.pack_id,
        scratch_path=bundle.scratch_path,
    )

    # Alpha-only: charge the action's credits and watch the USD budget. In debug
    # mode there is no budget, so neither charge nor alert (avoids "-$0.70" noise).
    if await modes.get_mode(db) == modes.ALPHA:
        if not _is_first_admin(user_id):
            left, spent = await db.consume_credits(user_id, cost)
            if spent:  # a no-op spend must not leave a refundable charge event
                await analytics.log(db, user_id, analytics.CREDITS_CHARGED, mode=mode, credits=cost)
            await state.update_data(bal=left)
            await msg.answer(
                f"Списано {pricing.format_packs(cost)} пак. "
                f"💎 Осталось: {pricing.format_packs(left)} (детали: /balance)."
            )
        try:
            alerts = await budget.pending_alerts(db)
        except Exception:  # a broken alert check must not break the user's flow
            logger.exception("budget alert check failed")
            alerts = []
        for alert in alerts:
            for admin_id in get_settings().admin_id_list:
                with contextlib.suppress(Exception):
                    await msg.bot.send_message(admin_id, alert)  # type: ignore[union-attr]


async def on_retry(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Re-run generation after a transient overload (armed button finished its countdown)."""
    tag_component("handlers.flow")
    user_id = callback.from_user.id if callback.from_user else 0
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    spawned = False
    try:
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=None)  # drop the button while retrying
        _spawn_generation(msg, state, orchestrator, db, user_id)
        spawned = True
    finally:
        if not spawned:
            _end_action(user_id)


async def on_retry_wait(callback: CallbackQuery) -> None:  # pragma: no cover
    """User tapped the still-counting-down button — ask them to wait it out."""
    tag_component("handlers.flow")
    await callback.answer("Ещё секунду — идёт отсчёт, потом можно повторить.")


async def _present_for_publish(  # pragma: no cover
    msg: Message,
    state: FSMContext,
    stickers: list[StickerInput],
    *,
    mode: str,
    title: str,
    pack_id: int | None = None,
    scratch_path: str | None = None,
) -> None:
    """Send the transparent preview sheet(s), then offer publish/download below them.

    Only pointers go into the FSM — the generated bytes already live on disk
    (draft pack for fresh/reuse, scratch dir for extend). Stuffing megabytes
    of PNGs into fsm.sqlite both leaked disk and blocked the event loop on
    every state write.
    """
    with contextlib.suppress(Exception):
        await msg.edit_text(_STAGE_TEXT["preview"])
    await state.update_data(
        stickers=None, mode=mode, pack_id=pack_id, pub_title=title, scratch_path=scratch_path
    )
    await state.set_state(NewPack.publish)
    # Drop the progress message, drop previews, then put controls *below* the previews.
    data = await state.get_data()
    cid = data.get("wizard_chat_id")
    mid = data.get("wizard_msg_id")
    if cid is not None and mid is not None:
        with contextlib.suppress(TelegramBadRequest):
            await msg.bot.delete_message(cid, mid)  # type: ignore[union-attr]
    # PIL composition over ~15 images is CPU work — off the event loop.
    sheets = await asyncio.to_thread(compose_preview, [img for img, _ in stickers])
    for i, sheet in enumerate(sheets, start=1):
        await msg.answer_document(BufferedInputFile(sheet, filename=f"preview_{i}.png"))
    sent = await msg.answer(
        f"Готово: {len(stickers)} стикеров. Что дальше?",
        reply_markup=publish_kb(),
    )
    await _store_wizard(state, sent)


async def _review_stickers(orchestrator: Orchestrator, data: dict[str, Any]) -> list[StickerInput]:
    """Load the reviewed stickers from their on-disk home (FSM holds pointers only).

    Falls back to raw bytes in the FSM for states written before this change,
    so in-flight reviews survive the deploy. The ``pack_id`` branch is valid
    ONLY outside extend mode: there ``pack_id`` is the draft holding exactly
    the reviewed stickers, while in extend mode it is the TARGET pack — loading
    that would re-publish the pack's own existing stickers (a stale review
    button mid-extend would silently double the whole set).
    """
    legacy: list[StickerInput] = data.get("stickers") or []
    if legacy:
        return legacy
    if scratch_path := data.get("scratch_path"):
        return await orchestrator.load_scratch(str(scratch_path))
    if data.get("mode") != "extend" and (pack_id := data.get("pack_id")):
        return await orchestrator.load_pack_stickers(int(pack_id))
    return []


async def on_publish_yes(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Publish the previewed stickers (new pack or extend)."""
    tag_component("handlers.flow")
    user_id = callback.from_user.id if callback.from_user else 0
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    try:
        data = await state.get_data()
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        stickers: list[StickerInput] = await _review_stickers(orchestrator, data)
        if not stickers:
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text(f"Нет готовых стикеров. {_TXT_NEW_OR_ADDTO}")
            await state.clear()
            return

        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text("📦 Публикую пак в Telegram…")
        try:
            async with _typing(msg):
                pack = await db.get_pack(int(data["pack_id"])) if data.get("pack_id") else None
                if pack is None:
                    raise RuntimeError("pack not found")
                # A non-extend publish targets a draft; if it is already
                # published (e.g. via the /mypacks button while this review
                # screen was still open), a second create would duplicate the
                # Telegram set. Extend legitimately targets a published pack.
                if data.get("mode") != "extend" and pack.published:
                    await state.clear()
                    with contextlib.suppress(TelegramBadRequest):
                        await msg.edit_text(f"Уже опубликован: {pack.link}")
                    return
                # Diagnostic: which branch a publish takes (extend vs new set) and on
                # which pack — so a "made a new set instead of extending" report is
                # traceable to the mode/pack_id the FSM actually held at publish time.
                logger.info(
                    "publish: mode=%s pack_id=%s set=%s stickers=%d",
                    data.get("mode"),
                    pack.id,
                    pack.set_name,
                    len(stickers),
                )
                if data.get("mode") == "extend":
                    result = await orchestrator.publish_extend(
                        owner_id=user_id,
                        pack=pack,
                        stickers=stickers,
                        # Extend has no draft rows, so captions ride from the FSM
                        # straight to the per-add persistence (history feature).
                        captions=selected_captions(data.get("std_sel", []), data.get("custom", [])),
                    )
                else:
                    result = await orchestrator.publish_draft(
                        owner_id=user_id, pack=pack, stickers=stickers
                    )
        except Exception as exc:
            logger.exception("publish failed")
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text(_friendly_error(exc))
            await _alert_owner_genfail(msg, user_id, "публикация пака", exc)
            return

        event = analytics.EXTENDED if data.get("mode") == "extend" else analytics.PUBLISHED
        await analytics.log(db, user_id, event, set_name=result.set_name, count=result.count)
        if scratch_path := data.get("scratch_path"):  # extend: published, bytes now redundant
            await orchestrator.drop_scratch(str(scratch_path))
        await state.clear()
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text(f"✅ Готово! Пак: {result.link}")
    finally:
        _end_action(user_id)


async def on_pub_download(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:
    """Send the stickers as a ZIP; keep state so the user can still publish."""
    tag_component("handlers.flow")
    user_id = callback.from_user.id if callback.from_user else 0
    # Single-flight: a zip is tens of MB of disk reads + PIL work; tap-spam on
    # this button must not stack unbounded memory peaks (same as publish).
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    try:
        data = await state.get_data()
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        stickers: list[StickerInput] = await _review_stickers(orchestrator, data)
        if not stickers:
            await msg.answer(f"Нет готовых стикеров. {_TXT_NEW_OR_ADDTO}")
            return
        if callback.from_user is not None:
            await analytics.log(
                db, callback.from_user.id, analytics.DOWNLOADED, count=len(stickers)
            )
        archive = await asyncio.to_thread(bundle_zip, [img for img, _ in stickers])
        await msg.answer_document(
            BufferedInputFile(archive, filename="stickers.zip"),
            caption="Готовые стикеры (PNG, 512px, прозрачный фон).",
        )
    finally:
        _end_action(user_id)


async def _drop_review_scratch(  # pragma: no cover
    orchestrator: Orchestrator, state: FSMContext
) -> None:
    """Best-effort removal of the review's scratch dir when a flow is abandoned."""
    with contextlib.suppress(Exception):
        if scratch_path := (await state.get_data()).get("scratch_path"):
            await orchestrator.drop_scratch(str(scratch_path))


async def on_publish_no(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator
) -> None:
    """Cancel."""
    tag_component("handlers.flow")
    await _drop_review_scratch(orchestrator, state)
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text(f"Отменено. {_TXT_NEW_OR_ADDTO}")


async def cmd_cancel(message: Message, state: FSMContext, orchestrator: Orchestrator) -> None:
    """Abort whatever the user is in the middle of (works from any FSM state)."""
    tag_component("handlers.flow")
    if await state.get_state() is None:
        await message.answer(f"Нечего отменять. {_TXT_NEW_OR_ADDTO}")
        return
    await _drop_review_scratch(orchestrator, state)
    await state.clear()
    await message.answer(f"Отменено. {_TXT_NEW_OR_ADDTO}")


# --- saved characters: list → actions (edit in place + back) -----------------


def _chars_markup(characters: list[Any]) -> Any:
    kb = InlineKeyboardBuilder()
    for char in characters:
        kb.button(text=f"{char.name} ({char.style_id})", callback_data=f"char:{char.id}")
    kb.adjust(1)
    return kb.as_markup()


_TXT_CHARS = (
    "Твои персонажи. Выбери, чтобы посмотреть каноникл, добавить стикеры "
    "(0.5 пака) или перерисовать (1 пак):"
)


def _char_actions_kb(char_id: int) -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="🖼 Показать каноникл", callback_data=f"canon:{char_id}")
    kb.button(
        text=f"➕ Добавить стикеры ({pricing.format_packs(pricing.COST_ADD_STICKERS)} пака)",
        callback_data=f"cadd:{char_id}",
    )
    kb.button(
        text=f"🔄 Перерисовать ({pricing.format_packs(pricing.COST_REDRAW)} пак)",
        callback_data=f"credraw:{char_id}",
    )
    kb.button(text="⬅️ К списку", callback_data="nav:chars")
    kb.adjust(1)
    return kb.as_markup()


async def cmd_mychars(message: Message, db: Database) -> None:
    """List saved characters; selecting one starts a new pack about them (§3.2)."""
    tag_component("handlers.flow")
    user_id = message.from_user.id if message.from_user else 0
    if (hint := await _alpha_gate(db, user_id)) is not None:
        await message.answer(hint)
        return
    characters = await db.list_characters(user_id)
    if not characters:
        await message.answer("Пока нет сохранённых персонажей. Создай пак: /new")
        return
    text = _TXT_CHARS
    if (wallet := await _alpha_wallet(db, user_id)).get("alpha"):
        text += f"\n\n💎 Баланс: {pricing.format_packs(wallet['bal'])} паков."
    await message.answer(text, reply_markup=_chars_markup(characters))


async def on_nav_chars(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Back to the character list (edits the menu message in place)."""
    tag_component("handlers.flow")
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    user_id = callback.from_user.id if callback.from_user else 0
    characters = await db.list_characters(user_id)
    if not characters:
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text("Пока нет сохранённых персонажей. Создай пак: /new")
        return
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(_TXT_CHARS, reply_markup=_chars_markup(characters))


async def on_pick_char(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    """Show actions for a saved character: show canonical / add stickers / redraw."""
    tag_component("handlers.flow")
    char_id = int((callback.data or "char:0").split(":", 1)[-1])
    char = await db.get_character(char_id)
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    if char is None:
        await msg.answer("Персонаж не найден.")
        return
    await _store_wizard(state, msg)
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(
            f"«{char.name}» ({char.style_id}). Что сделать?", reply_markup=_char_actions_kb(char_id)
        )


async def on_show_canonical(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Send the character's saved canonical image (keeps the menu in place)."""
    tag_component("handlers.flow")
    char_id = int((callback.data or "canon:0").split(":", 1)[-1])
    char = await db.get_character(char_id)
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    if char is None:
        await msg.answer("Персонаж не найден.")
        return
    try:
        data = Path(char.canonical_path).read_bytes()
    except OSError:
        await msg.answer("Каноникл недоступен. Попробуй перерисовать его.")
        return
    await msg.answer_document(
        BufferedInputFile(data, filename=f"{char.name}.png"),
        caption=f"Каноникл «{char.name}» ({char.style_id}).",
    )


async def on_char_add(
    callback: CallbackQuery, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:  # pragma: no cover
    """Add stickers to a saved character → caption selection → create (0.5 pack)."""
    tag_component("handlers.flow")
    char_id = int((callback.data or "cadd:0").split(":", 1)[-1])
    await _enter_captions(callback, state, db, orchestrator, mode="reuse", character_id=char_id)


async def on_char_redraw(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:
    """Start redrawing a character's canonical: charge gate, then ask for a new photo."""
    tag_component("handlers.flow")
    char_id = int((callback.data or "credraw:0").split(":", 1)[-1])
    user_id = callback.from_user.id if callback.from_user else 0
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    char = await db.get_character(char_id)
    if char is None:
        await msg.answer("Персонаж не найден.")
        return
    if (hint := await _generation_gate(db, user_id, pricing.COST_REDRAW)) is not None:
        await msg.answer(hint)
        return
    await _drop_review_scratch(orchestrator, state)
    await state.clear()
    await state.update_data(redraw_char_id=char_id)
    await state.set_state(Redraw.photo)
    prompt = (
        f"🔄 Перерисовка «{char.name}» спишет {pricing.format_packs(pricing.COST_REDRAW)} пак. "
        "Пришли новое фото человека."
    )
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(prompt)
    await _store_wizard(state, msg)


async def on_redraw_photo(  # pragma: no cover
    message: Message, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Receive the new photo, rebuild the canonical, replace it, and charge a pack."""
    tag_component("handlers.flow")
    if not message.photo:
        await message.answer("Нужно именно фото. Пришли изображение человека.")
        return
    data = await state.get_data()
    char = await db.get_character(int(data.get("redraw_char_id", 0)))
    user_id = message.from_user.id if message.from_user else 0
    if char is None:
        await state.clear()
        await message.answer("Персонаж не найден. Начни заново: /mychars")
        return
    await message.bot.send_chat_action(message.chat.id, "typing")  # type: ignore[union-attr]
    status = await _replace_below(message, state, "📸 Принял фото, проверяю…")
    photo = await _download_photo(message, status)
    if photo is None:
        return  # download failed → user was told to resend
    try:
        async with _photo_check_timeout():
            code = await orchestrator.validate_photo(photo)
    except Exception as exc:  # vision check failed/stalled — let it through rather than block
        logger.warning("photo check failed: %s", str(exc)[:100])
        code = None
    if code == photo_check.NUDE:
        with contextlib.suppress(TelegramBadRequest):
            await status.delete()
        await _strike(db, user_id, message, "На фото обнажёнка")
        return
    if code is not None:
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text(_PHOTO_HINTS.get(code, "Фото не подходит, пришли другое."))
        return
    with contextlib.suppress(TelegramBadRequest):
        await status.edit_text("🎨 Перерисовываю персонажа…")

    async def on_step(done: int, total: int) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(f"🎨 Перерисовываю… {_progress_bar(done, total)} {done}/{total}")

    try:
        async with _typing(message), _generation_timeout():
            canonical = await orchestrator.redraw_canonical(char, photo, on_step=on_step)
    except Exception as exc:
        logger.exception("redraw failed")
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text(_friendly_error(exc))
        await _alert_owner_genfail(message, user_id, "перерисовка персонажа", exc)
        if model_errors.is_quota(exc):
            await _alert_admins_quota(message, exc)
        return
    await state.clear()
    with contextlib.suppress(TelegramBadRequest):
        await status.delete()
    await message.answer_document(
        BufferedInputFile(canonical, filename=f"{char.name}.png"),
        caption=f"✅ Готово, новый каноникл «{char.name}».",
    )
    if not _is_first_admin(user_id) and await modes.get_mode(db) == modes.ALPHA:
        left, spent = await db.consume_credits(user_id, pricing.COST_REDRAW)
        if spent:  # a no-op spend must not leave a refundable charge event
            await analytics.log(
                db, user_id, analytics.CREDITS_CHARGED, mode="redraw", credits=pricing.COST_REDRAW
            )
        await message.answer(
            f"Списано {pricing.format_packs(pricing.COST_REDRAW)} пак. "
            f"Осталось: {pricing.format_packs(left)}."
        )


async def cmd_addto(message: Message, db: Database) -> None:
    """List the user's PUBLISHED packs; selecting one appends to that same set (§3.2)."""
    tag_component("handlers.flow")
    user_id = message.from_user.id if message.from_user else 0
    if (hint := await _alpha_gate(db, user_id)) is not None:
        await message.answer(hint)
        return
    packs = [p for p in await db.list_packs(user_id) if p.published]
    if not packs:
        await message.answer("Пока нет опубликованных паков для дополнения. Создай пак: /new")
        return
    kb = InlineKeyboardBuilder()
    for pack in packs:
        kb.button(text=pack.title, callback_data=f"extend:{pack.id}")
    kb.adjust(1)
    text = (
        "Дополнить пак (тем же персонажем — новое фото нельзя, иначе пак станет "
        f"разнородным). Это стоит {pricing.format_packs(pricing.COST_ADD_STICKERS)} пака:"
    )
    if (wallet := await _alpha_wallet(db, user_id)).get("alpha"):
        text += f"\n\n💎 Баланс: {pricing.format_packs(wallet['bal'])} паков."
    await message.answer(text, reply_markup=kb.as_markup())


# --- saved packs: list → actions (edit in place + back) ----------------------


def _packs_markup(packs: list[Any]) -> Any:
    kb = InlineKeyboardBuilder()
    for pack in packs:
        mark = "✅" if pack.published else "📝"
        kb.button(text=f"{mark} {pack.title}", callback_data=f"pk:{pack.id}")
    kb.adjust(1)
    return kb.as_markup()


_TXT_PACKS = (
    "Ваши паки. Выберите пак, чтобы открыть, опубликовать или скачать; "
    "✅ — опубликованный, 📝 — черновик. Дополнить пак — /addto, новый — /new, "
    "что умею и цены — /help.\n\n🐞 Если что-то не так — напишите /report."
)


async def cmd_mypacks(message: Message, db: Database) -> None:
    """List saved packs: published → open/download, drafts → publish/download."""
    tag_component("handlers.flow")
    user_id = message.from_user.id if message.from_user else 0
    if (hint := await _alpha_gate(db, user_id)) is not None:
        await message.answer(hint)
        return
    packs = await db.list_packs(user_id)
    if not packs:
        await message.answer("Пока нет сохранённых паков. Создай: /new")
        return
    text = _TXT_PACKS
    if (wallet := await _alpha_wallet(db, user_id)).get("alpha"):
        text += f"\n💎 Баланс: {pricing.format_packs(wallet['bal'])} паков."
    await message.answer(text, reply_markup=_packs_markup(packs))


async def on_nav_packs(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Back to the pack list (edits the menu message in place)."""
    tag_component("handlers.flow")
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    user_id = callback.from_user.id if callback.from_user else 0
    packs = await db.list_packs(user_id)
    if not packs:
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text("Пока нет сохранённых паков. Создай: /new")
        return
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(_TXT_PACKS, reply_markup=_packs_markup(packs))


async def on_pick_saved_pack(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Show actions for a saved pack: open/download (published) or publish/download (draft)."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "pk:0").split(":", 1)[-1])
    pack = await db.get_pack(pack_id)
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    if pack is None:
        await msg.answer("Пак не найден")
        return
    kb = InlineKeyboardBuilder()
    if pack.published:
        kb.button(text="🔗 Открыть", url=pack.link)
    else:
        kb.button(text="✅ Опубликовать", callback_data=f"pkpub:{pack.id}")
    kb.button(text="⬇️ Скачать (zip)", callback_data=f"pkdl:{pack.id}")
    kb.button(text="📝 Описания", callback_data=f"pkcap:{pack.id}")
    kb.button(text="⬅️ К списку", callback_data="nav:packs")
    kb.adjust(1)
    status = "опубликован" if pack.published else "черновик"
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(f"«{pack.title}» — {status}.", reply_markup=kb.as_markup())


async def on_saved_captions(callback: CallbackQuery, db: Database) -> None:
    """📝 Numbered idea/caption list for one saved pack (stored per sticker)."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "pkcap:0").split(":", 1)[-1])
    user_id = callback.from_user.id if callback.from_user else 0
    pack = await db.get_pack(pack_id)
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None or pack is None or pack.owner_id != user_id:
        return
    rows = await db.list_stickers(pack_id)
    if not rows:
        await msg.answer("В этом паке нет сохранённых стикеров.")
        return
    lines = [
        # NULL caption = the pack predates the history feature (12.06).
        f"{i}. {s.emoji} {s.caption or '— (пак создан до появления истории)'}"
        for i, s in enumerate(rows, 1)
    ]
    await msg.answer((f"📝 «{pack.title}» — идеи стикеров:\n" + "\n".join(lines))[:4000])


async def cmd_history(message: Message, db: Database) -> None:
    """Show the user's recent caption orders — the generation history."""
    tag_component("handlers.flow")
    user_id = message.from_user.id if message.from_user else 0
    if (hint := await _alpha_gate(db, user_id)) is not None:
        await message.answer(hint)
        return
    entries = await db.events_for(user_id, analytics.CAPTIONS_SELECTED, limit=5)
    if not entries:
        await message.answer("История пуста — закажи первый пак: /new")
        return
    blocks: list[str] = []
    for created, detail in entries:
        standard = detail.get("standard") or []
        custom = detail.get("custom") or []
        texts = [str(t) for t in [*standard, *custom][:MAX_CAPTIONS]]  # type: ignore[union-attr]
        listing = "\n".join(f"  {i}. {t}" for i, t in enumerate(texts, 1))
        blocks.append(f"🗓 {created.strftime('%d.%m %H:%M')} UTC — {len(texts)} шт.\n{listing}")
    text = "📝 Последние заказы стикеров (до 5):\n\n" + "\n\n".join(blocks)
    await message.answer(text[:4000])


async def on_saved_download(  # pragma: no cover
    callback: CallbackQuery, db: Database, orchestrator: Orchestrator
) -> None:
    """Download a saved pack's stickers as a ZIP (any number of times)."""
    tag_component("handlers.flow")
    user_id = callback.from_user.id if callback.from_user else 0
    # Single-flight: a 120-sticker pack zip holds ~tens of MB in RAM per tap.
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    try:
        pack_id = int((callback.data or "pkdl:0").split(":", 1)[-1])
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        stickers = await orchestrator.load_pack_stickers(pack_id)
        if not stickers:
            await msg.answer("У этого пака нет сохранённых стикеров.")
            return
        if callback.from_user is not None:
            await analytics.log(
                db,
                callback.from_user.id,
                analytics.DOWNLOADED,
                pack_id=pack_id,
                count=len(stickers),
            )
        archive = await asyncio.to_thread(bundle_zip, [img for img, _ in stickers])
        await msg.answer_document(
            BufferedInputFile(archive, filename="stickers.zip"),
            caption="Готовые стикеры (PNG, 512px, прозрачный фон).",
        )
    finally:
        _end_action(user_id)


async def on_saved_publish(  # pragma: no cover
    callback: CallbackQuery, db: Database, orchestrator: Orchestrator
) -> None:
    """Publish a saved draft pack (only if not already published)."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "pkpub:0").split(":", 1)[-1])
    user_id = callback.from_user.id if callback.from_user else 0
    # Single-flight: a double-tap here would create two identical Telegram sets.
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    try:
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        pack = await db.get_pack(pack_id)
        if pack is None:
            await msg.answer("Пак не найден")
            return
        if pack.published:
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text(f"Уже опубликован: {pack.link}")
            return
        stickers = await orchestrator.load_pack_stickers(pack_id)
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text("📦 Публикую пак в Telegram…")
        try:
            async with _typing(msg):
                result = await orchestrator.publish_draft(
                    owner_id=user_id, pack=pack, stickers=stickers
                )
        except Exception as exc:
            logger.exception("publish failed")
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text(_friendly_error(exc))
            await _alert_owner_genfail(msg, user_id, "публикация сохранённого пака", exc)
            return
        await analytics.log(
            db, user_id, analytics.PUBLISHED, set_name=result.set_name, count=result.count
        )
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text(f"✅ Готово! Пак: {result.link}")
    finally:
        _end_action(user_id)


async def on_pick_pack(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:
    """Extend an existing pack → caption selection → create → append."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "extend:0").split(":", 1)[-1])
    # Refuse a full pack up front, so the user never builds captions for a set
    # that can't take any more stickers (the 120-limit is enforced again, for
    # free, at generation time if they pick more than the remaining room).
    room = remaining_capacity(await db.count_stickers(pack_id))
    if room == 0:
        msg = callback.message if isinstance(callback.message, Message) else None
        await callback.answer()
        if msg is not None:
            pack = await db.get_pack(pack_id)
            title = pack.title if pack else "этот"
            await msg.answer(f"Пак «{title}» уже заполнен (120/120). Создай новый пак: /new")
        return
    await _enter_captions(callback, state, db, orchestrator, mode="extend", pack_id=pack_id)


def build_router() -> Router:
    """Build a fresh flow router (factory: safe to call per dispatcher)."""
    router = Router(name="flow")
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_mychars, Command("mychars"))
    router.message.register(cmd_mypacks, Command("mypacks"))
    router.message.register(cmd_history, Command("history"))
    router.message.register(cmd_addto, Command("addto"))
    router.message.register(on_photo, NewPack.photo)
    router.message.register(on_name, NewPack.name)
    router.callback_query.register(on_nav_back, F.data == "nav:back")
    router.callback_query.register(on_nav_cancel, F.data == "nav:cancel")
    # Forward wizard buttons require THEIR step's state: a tap on a button from
    # a finished pack's old message (the flow data is gone) must not walk the
    # wizard into a crash — it falls through to the stale-button catch-all.
    router.callback_query.register(on_subject, NewPack.subject, F.data.startswith("subject:"))
    router.callback_query.register(on_age, NewPack.child_age, F.data.startswith("age:"))
    router.callback_query.register(on_styles_exp, NewPack.style, F.data == "styles:exp")
    router.callback_query.register(on_styles_main, NewPack.style, F.data == "styles:main")
    router.callback_query.register(on_style, NewPack.style, F.data.startswith("style:"))
    router.callback_query.register(on_std_toggle, NewPack.select_std, F.data.startswith("std:"))
    router.callback_query.register(on_random_idea, NewPack.enter_custom, F.data == "randidea")
    router.callback_query.register(on_random_take, NewPack.enter_custom, F.data == "randtake")
    router.callback_query.register(
        on_std_bulk, NewPack.select_std, F.data.in_({"stdall", "stdclear"})
    )
    router.callback_query.register(on_std_page, NewPack.select_std, F.data.startswith("stdpage:"))
    router.callback_query.register(on_std_done, NewPack.select_std, F.data == "stddone")
    router.callback_query.register(on_custom_yes, NewPack.ask_custom, F.data == "cust:yes")
    router.callback_query.register(on_custom_no, NewPack.ask_custom, F.data == "cust:no")
    router.message.register(on_enter_custom, NewPack.enter_custom)
    router.callback_query.register(on_rev_create, NewPack.review, F.data == "rev:create")
    router.callback_query.register(on_retry, NewPack.review, F.data == "retry:gen")
    router.callback_query.register(on_retry_wait, F.data == "retry:wait")
    router.callback_query.register(on_rev_add, NewPack.review, F.data == "rev:add")
    router.callback_query.register(on_rev_remove, NewPack.review, F.data == "rev:remove")
    router.callback_query.register(on_rev_show, NewPack.review, F.data == "rev:show")
    router.callback_query.register(on_rem_pick, NewPack.review, F.data.startswith("rem:"))
    router.callback_query.register(on_publish_yes, NewPack.publish, F.data == "pub:yes")
    router.callback_query.register(on_pub_download, NewPack.publish, F.data == "pub:dl")
    router.callback_query.register(on_publish_no, NewPack.publish, F.data == "pub:no")
    router.callback_query.register(on_nav_chars, F.data == "nav:chars")
    router.callback_query.register(on_nav_packs, F.data == "nav:packs")
    router.callback_query.register(on_pick_char, F.data.startswith("char:"))
    router.callback_query.register(on_show_canonical, F.data.startswith("canon:"))
    router.callback_query.register(on_char_add, F.data.startswith("cadd:"))
    router.callback_query.register(on_char_redraw, F.data.startswith("credraw:"))
    router.message.register(on_redraw_photo, Redraw.photo)
    router.callback_query.register(on_pick_pack, F.data.startswith("extend:"))
    router.callback_query.register(on_pick_saved_pack, F.data.startswith("pk:"))
    router.callback_query.register(on_saved_publish, F.data.startswith("pkpub:"))
    router.callback_query.register(on_saved_download, F.data.startswith("pkdl:"))
    router.callback_query.register(on_saved_captions, F.data.startswith("pkcap:"))
    # LAST: anything wizard-shaped that no state filter accepted is stale.
    router.callback_query.register(on_stale_wizard, F.func(lambda c: _is_wizard_callback(c.data)))
    return router
