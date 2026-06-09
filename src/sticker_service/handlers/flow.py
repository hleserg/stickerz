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
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services import analytics, budget, modes, photo_check, pricing
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.models import errors as model_errors
from sticker_service.services.moderation import caption_rejection_reason
from sticker_service.services.orchestrator import Orchestrator
from sticker_service.services.postprocess import bundle_zip, compose_preview
from sticker_service.services.publish.publisher import StickerInput
from sticker_service.services.stickers import MAX_CAPTIONS, STANDARD_BLOCK, selected_captions
from sticker_service.services.strikes import register_strike

logger = logging.getLogger(__name__)

_STAGE_TEXT = {
    "sheet": "✨ Рисую лист стикеров…",
    "slice": "✂️ Нарезаю на отдельные стикеры…",
    "emoji": "🎭 Подбираю эмодзи…",
    "publish": "📦 Публикую пак в Telegram…",
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
_TXT_CAPTIONS = (
    "Выберите стандартные стикеры для набора. Дальше можно добавить свои, "
    "но всего не больше 15 (один лист)."
)
_TXT_ASK_CUSTOM = "Хотите добавить свои подписи?"
_TXT_ENTER_CUSTOM = "Напиши свою подпись сообщением (текст для стикера):"


def _friendly_error(exc: Exception) -> str:
    """Turn a backend exception into a short user-facing message (shared taxonomy)."""
    return model_errors.user_message(exc)


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    """Render a ``▰▰▱▱▱`` bar for a done/total step count."""
    total = max(total, 1)
    filled = round(width * max(0, min(done, total)) / total)
    return "▰" * filled + "▱" * (width - filled)


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
    kb = InlineKeyboardBuilder()
    for style_id, display in loader.menu():
        kb.button(text=display, callback_data=f"style:{style_id}")
    kb.adjust(1)
    _attach_nav(kb, back=True)
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
    from sticker_service.services.stickers import PER_PAGE, STANDARD_BLOCK

    kb = InlineKeyboardBuilder()
    pages = max(1, (len(STANDARD_BLOCK) + PER_PAGE - 1) // PER_PAGE)
    start = page * PER_PAGE
    page_items = list(enumerate(STANDARD_BLOCK))[start : start + PER_PAGE]
    for i, caption in page_items:
        mark = "✅" if i in selected else "⬜"
        kb.button(text=f"{mark} {caption}", callback_data=f"std:{i}")
    kb.adjust(3)  # 3 columns → up to 3×4 per page
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
        return _TXT_CAPTIONS, std_checklist_kb(selected, page)
    if target == NewPack.ask_custom.state:
        return _TXT_ASK_CUSTOM, ask_custom_kb()
    if target == NewPack.enter_custom.state:
        return _TXT_ENTER_CUSTOM, _nav_kb(back=True)
    if target == NewPack.review.state:
        captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
        return _review_text(captions), review_kb(len(captions))
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


async def _alpha_gate(db: Database, user_id: int) -> str | None:  # pragma: no cover
    """In alpha, non-approved users must apply first."""
    if _is_admin(user_id) or await modes.get_mode(db) != modes.ALPHA:
        return None
    if not await db.is_allowed(user_id):
        return "🔒 Бот в альфа-тесте. Оставьте заявку через /start — мы пригласим вас."
    return None


async def _generation_gate(db: Database, user_id: int, cost: int) -> str | None:  # pragma: no cover
    """Budget + per-user credit check for an action costing ``cost`` credits."""
    if _is_admin(user_id) or await modes.get_mode(db) != modes.ALPHA:
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


async def cmd_new(message: Message, state: FSMContext, db: Database) -> None:
    """Start a new pack: record implicit photo-rights consent, then ask for a photo.

    Sending a photo to the bot is itself the consent act (§15.2): we record the
    fact + timestamp here instead of nagging with a separate confirmation step.
    """
    tag_component("handlers.flow")
    uid = message.from_user.id if message.from_user else 0
    if (hint := await _alpha_gate(db, uid)) is not None:
        await message.answer(hint)
        return
    await state.clear()
    if uid:
        await db.record_consent(uid)
    await state.set_state(NewPack.photo)
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


async def _enter_captions(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, *, mode: str, **extra: Any
) -> None:
    """Seed caption state (all standard selected) and show the checklist."""
    await state.update_data(
        mode=mode, std_sel=list(range(len(STANDARD_BLOCK))), custom=[], page=0, **extra
    )
    await callback.answer()
    await _show(callback, state, NewPack.select_std.state, None)


async def on_style(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:  # pragma: no cover
    """Store the style and start caption selection (canonical is built on Create)."""
    tag_component("handlers.flow")
    style_id = (callback.data or "").split(":", 1)[-1]
    if callback.from_user is not None:
        await analytics.log(db, callback.from_user.id, analytics.STYLE_CHOSEN, style_id=style_id)
    await _enter_captions(callback, state, mode="fresh", style_id=style_id)


async def on_std_toggle(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    idx = int((callback.data or "std:0").split(":", 1)[-1])
    data = await state.get_data()
    selected = list(data.get("std_sel", []))
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)
    await state.update_data(std_sel=selected)
    page = int(data.get("page", 0))
    with contextlib.suppress(Exception):
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=std_checklist_kb(selected, page))
    await callback.answer()


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
    custom = list(data.get("custom", []))
    total = len(selected_captions(data.get("std_sel", []), custom))
    if text and total < MAX_CAPTIONS:
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


async def on_nav_back(callback: CallbackQuery, state: FSMContext, loader: StyleLoader) -> None:
    """``⬅️ Назад`` — re-render the previous wizard step in place."""
    tag_component("handlers.flow")
    data = await state.get_data()
    target = _prev_state(await state.get_state(), data)
    await callback.answer()
    if target is not None:
        await _show(callback, state, target, loader, data)


async def on_nav_cancel(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """``❌ Отмена`` — abort the flow and collapse the wizard message."""
    tag_component("handlers.flow")
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text("Отменено. Новый пак: /new")


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

        # Inform of the price before doing the (paid) work, for alpha participants.
        if not _is_admin(user_id) and await modes.get_mode(db) == modes.ALPHA:
            have = await db.credits_left(user_id)
            await msg.answer(
                f"💸 Это действие спишет {pricing.format_packs(cost)} пак "
                f"(сейчас у тебя {pricing.format_packs(have)})."
            )

        std_names = [STANDARD_BLOCK[i] for i in sorted(set(data.get("std_sel", [])))]
        await analytics.log(
            db,
            user_id,
            analytics.CAPTIONS_SELECTED,
            standard=std_names,
            custom=list(data.get("custom", [])),
            total=len(captions),
        )
        await _generate_and_present(msg, state, orchestrator, db, user_id)
    finally:
        _end_action(user_id)


# --- generation core + retry-on-overload (shared by create & retry) ----------

_RETRY_DELAY_S = 20
_bg_tasks: set[asyncio.Task[None]] = set()


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
    cost = pricing.cost_for_mode(mode)
    started = time.monotonic()
    await state.set_state(NewPack.publish)

    async def on_step(done: int, total: int) -> None:
        with contextlib.suppress(Exception):
            await msg.edit_text(f"🎨 Рисую персонажа… {_progress_bar(done, total)} {done}/{total}")

    async def on_stage(label: str) -> None:
        with contextlib.suppress(Exception):
            await msg.edit_text(_STAGE_TEXT.get(label, "Работаю…"))

    with contextlib.suppress(Exception):
        await msg.edit_text("🎨 Рисую персонажа…")
    try:
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
        if model_errors.is_quota(exc):
            await _alert_admins_quota(msg, exc)
        if model_errors.is_retryable(exc):  # overload/timeout → offer a retry once it cools down
            _arm_retry(msg)
        return

    await analytics.log(
        db,
        user_id,
        analytics.GENERATION_DONE,
        mode=mode,
        count=len(bundle.stickers),
        seconds=round(time.monotonic() - started, 1),
    )
    await _present_for_publish(
        msg, state, bundle.stickers, mode=mode, title=bundle.title, pack_id=bundle.pack_id
    )

    # Alpha-only: charge the action's credits and watch the USD budget. In debug
    # mode there is no budget, so neither charge nor alert (avoids "-$0.70" noise).
    if await modes.get_mode(db) == modes.ALPHA:
        if not _is_admin(user_id):
            left = await db.consume_credits(user_id, cost)
            await msg.answer(
                f"Списано {pricing.format_packs(cost)} пак. Осталось: {pricing.format_packs(left)}."
            )
        for alert in await budget.pending_alerts(db):
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
    try:
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=None)  # drop the button while retrying
        await _generate_and_present(msg, state, orchestrator, db, user_id)
    finally:
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
) -> None:
    """Send the transparent preview sheet(s), then offer publish/download below them."""
    await state.update_data(stickers=stickers, mode=mode, pack_id=pack_id, pub_title=title)
    await state.set_state(NewPack.publish)
    # Drop the progress message, drop previews, then put controls *below* the previews.
    data = await state.get_data()
    cid = data.get("wizard_chat_id")
    mid = data.get("wizard_msg_id")
    if cid is not None and mid is not None:
        with contextlib.suppress(TelegramBadRequest):
            await msg.bot.delete_message(cid, mid)  # type: ignore[union-attr]
    for i, sheet in enumerate(compose_preview([img for img, _ in stickers]), start=1):
        await msg.answer_document(BufferedInputFile(sheet, filename=f"preview_{i}.png"))
    sent = await msg.answer(
        f"Готово: {len(stickers)} стикеров (прозрачный фон). Что дальше?",
        reply_markup=publish_kb(),
    )
    await _store_wizard(state, sent)


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
        stickers: list[StickerInput] = data.get("stickers") or []
        if not stickers:
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text("Нет готовых стикеров. Начни заново: /new")
            await state.clear()
            return

        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text("📦 Публикую пак в Telegram…")
        try:
            async with _typing(msg):
                pack = await db.get_pack(int(data["pack_id"])) if data.get("pack_id") else None
                if pack is None:
                    raise RuntimeError("pack not found")
                if data.get("mode") == "extend":
                    result = await orchestrator.publish_extend(
                        owner_id=user_id, pack=pack, stickers=stickers
                    )
                else:
                    result = await orchestrator.publish_draft(
                        owner_id=user_id, pack=pack, stickers=stickers
                    )
        except Exception as exc:
            logger.exception("publish failed")
            with contextlib.suppress(TelegramBadRequest):
                await msg.edit_text(_friendly_error(exc))
            return

        event = analytics.EXTENDED if data.get("mode") == "extend" else analytics.PUBLISHED
        await analytics.log(db, user_id, event, set_name=result.set_name, count=result.count)
        await state.clear()
        with contextlib.suppress(TelegramBadRequest):
            await msg.edit_text(f"✅ Готово! Пак: {result.link}")
    finally:
        _end_action(user_id)


async def on_pub_download(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:
    """Send the stickers as a ZIP; keep state so the user can still publish."""
    tag_component("handlers.flow")
    data = await state.get_data()
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    stickers: list[StickerInput] = data.get("stickers") or []
    if not stickers:
        await msg.answer("Нет готовых стикеров. Начни заново: /new")
        return
    if callback.from_user is not None:
        await analytics.log(db, callback.from_user.id, analytics.DOWNLOADED, count=len(stickers))
    archive = bundle_zip([img for img, _ in stickers])
    await msg.answer_document(
        BufferedInputFile(archive, filename="stickers.zip"),
        caption="Готовые стикеры (PNG, 512px, прозрачный фон).",
    )


async def on_publish_no(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Cancel."""
    tag_component("handlers.flow")
    await state.clear()
    await callback.answer()
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_text("Отменено. Новый пак: /new")


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Abort whatever the user is in the middle of (works from any FSM state)."""
    tag_component("handlers.flow")
    if await state.get_state() is None:
        await message.answer("Нечего отменять. Новый пак: /new")
        return
    await state.clear()
    await message.answer("Отменено. Начать заново: /new")


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
    await message.answer(_TXT_CHARS, reply_markup=_chars_markup(characters))


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


async def on_char_add(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Add stickers to a saved character → caption selection → create (0.5 pack)."""
    tag_component("handlers.flow")
    char_id = int((callback.data or "cadd:0").split(":", 1)[-1])
    await _enter_captions(callback, state, mode="reuse", character_id=char_id)


async def on_char_redraw(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, db: Database
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
    if not _is_admin(user_id) and await modes.get_mode(db) == modes.ALPHA:
        left = await db.consume_credits(user_id, pricing.COST_REDRAW)
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
    await message.answer(
        "Дополнить пак (тем же персонажем — новое фото нельзя, иначе пак станет разнородным):",
        reply_markup=kb.as_markup(),
    )


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
    await message.answer(_TXT_PACKS, reply_markup=_packs_markup(packs))


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
    kb.button(text="⬅️ К списку", callback_data="nav:packs")
    kb.adjust(1)
    status = "опубликован" if pack.published else "черновик"
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(f"«{pack.title}» — {status}.", reply_markup=kb.as_markup())


async def on_saved_download(  # pragma: no cover
    callback: CallbackQuery, db: Database, orchestrator: Orchestrator
) -> None:
    """Download a saved pack's stickers as a ZIP (any number of times)."""
    tag_component("handlers.flow")
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
            db, callback.from_user.id, analytics.DOWNLOADED, pack_id=pack_id, count=len(stickers)
        )
    archive = bundle_zip([img for img, _ in stickers])
    await msg.answer_document(
        BufferedInputFile(archive, filename="stickers.zip"),
        caption="Готовые стикеры (PNG, 512px, прозрачный фон).",
    )


async def on_saved_publish(  # pragma: no cover
    callback: CallbackQuery, db: Database, orchestrator: Orchestrator
) -> None:
    """Publish a saved draft pack (only if not already published)."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "pkpub:0").split(":", 1)[-1])
    user_id = callback.from_user.id if callback.from_user else 0
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
        return
    await analytics.log(
        db, user_id, analytics.PUBLISHED, set_name=result.set_name, count=result.count
    )
    with contextlib.suppress(TelegramBadRequest):
        await msg.edit_text(f"✅ Готово! Пак: {result.link}")


async def on_pick_pack(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Extend an existing pack → caption selection → create → append."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "extend:0").split(":", 1)[-1])
    await _enter_captions(callback, state, mode="extend", pack_id=pack_id)


def build_router() -> Router:
    """Build a fresh flow router (factory: safe to call per dispatcher)."""
    router = Router(name="flow")
    router.message.register(cmd_new, Command("new"))
    router.message.register(cmd_cancel, Command("cancel"))
    router.message.register(cmd_mychars, Command("mychars"))
    router.message.register(cmd_mypacks, Command("mypacks"))
    router.message.register(cmd_addto, Command("addto"))
    router.message.register(on_photo, NewPack.photo)
    router.message.register(on_name, NewPack.name)
    router.callback_query.register(on_nav_back, F.data == "nav:back")
    router.callback_query.register(on_nav_cancel, F.data == "nav:cancel")
    router.callback_query.register(on_subject, F.data.startswith("subject:"))
    router.callback_query.register(on_age, F.data.startswith("age:"))
    router.callback_query.register(on_style, F.data.startswith("style:"))
    router.callback_query.register(on_std_toggle, F.data.startswith("std:"))
    router.callback_query.register(on_std_page, F.data.startswith("stdpage:"))
    router.callback_query.register(on_std_done, F.data == "stddone")
    router.callback_query.register(on_custom_yes, F.data == "cust:yes")
    router.callback_query.register(on_custom_no, F.data == "cust:no")
    router.message.register(on_enter_custom, NewPack.enter_custom)
    router.callback_query.register(on_rev_create, F.data == "rev:create")
    router.callback_query.register(on_retry, F.data == "retry:gen")
    router.callback_query.register(on_retry_wait, F.data == "retry:wait")
    router.callback_query.register(on_rev_add, F.data == "rev:add")
    router.callback_query.register(on_rev_remove, F.data == "rev:remove")
    router.callback_query.register(on_rev_show, F.data == "rev:show")
    router.callback_query.register(on_rem_pick, F.data.startswith("rem:"))
    router.callback_query.register(on_publish_yes, F.data == "pub:yes")
    router.callback_query.register(on_pub_download, F.data == "pub:dl")
    router.callback_query.register(on_publish_no, F.data == "pub:no")
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
    return router
