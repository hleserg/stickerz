"""Conversational pack-building flow (§3.1): new/existing character, extend.

This is the aiogram I/O shell that drives the user through photo → params →
style → canonical → confirm → published pack, and the "add to pack" branch.
Business logic lives in the tested service layer (orchestrator, engine,
postprocess); this module wires Telegram interaction to it.

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
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services import analytics, budget, modes, photo_check
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


# --- keyboards (pure) --------------------------------------------------------


def subject_kb() -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="Взрослый", callback_data="subject:adult")
    kb.button(text="Ребёнок", callback_data="subject:child")
    return kb.as_markup()


def age_kb() -> Any:
    kb = InlineKeyboardBuilder()
    for age in range(19):  # 0..18 (§5.3)
        kb.button(text=str(age), callback_data=f"age:{age}")
    kb.adjust(5)
    return kb.as_markup()


def style_kb(loader: StyleLoader) -> Any:
    kb = InlineKeyboardBuilder()
    for style_id, display in loader.menu():
        kb.button(text=display, callback_data=f"style:{style_id}")
    kb.adjust(1)
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
    """Checklist of standard captions: 12 per page (3×4), toggles + nav + done."""
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
    return kb.as_markup()


def ask_custom_kb() -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да", callback_data="cust:yes")
    kb.button(text="Нет", callback_data="cust:no")
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
    return kb.as_markup()


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


async def _generation_gate(db: Database, user_id: int) -> str | None:  # pragma: no cover
    """Budget + per-user generation limits for approved alpha participants."""
    if _is_admin(user_id) or await modes.get_mode(db) != modes.ALPHA:
        return None
    if not await budget.enough_for(db, 2):
        return (
            "⛔ Тестирование временно приостановлено из-за исчерпания бюджета. "
            "Скоро либо пополним бюджет, либо перейдём в бета-стадию — ждите уведомлений."
        )
    if await db.generations_left(user_id) <= 0:
        return (
            "К сожалению, генерации для тестирования закончились. "
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
    await message.answer(
        "Создаём новый пак. Пришли фото человека.\n\n"
        "🐞 Заметишь любой косяк — пожалуйста, напиши /report с подробностями "
        "(что делал, что не так): это очень помогает улучшать бота."
    )


_PHOTO_HINTS = {
    photo_check.NO_PERSON: "Не вижу человека на фото. Пришли фото, где есть человек.",
    photo_check.MULTI: "На фото больше одного человека. Нужен ровно один.",
    photo_check.SMALL: "Человек слишком мелкий — пусть занимает хотя бы 1/5 кадра.",
}


async def on_photo(  # pragma: no cover
    message: Message, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Accept + validate the photo (vision foolproof check), then ask for a name."""
    tag_component("handlers.flow")
    if not message.photo:
        await message.answer("Нужно именно фото. Пришли изображение человека.")
        return
    bot = message.bot
    file = await bot.download(message.photo[-1].file_id)  # type: ignore[union-attr]
    photo = file.read() if file else b""
    uid = message.from_user.id if message.from_user else 0
    try:
        code = await orchestrator.validate_photo(photo)
    except Exception as exc:  # vision check failed — let it through rather than block
        logger.warning("photo check failed: %s", str(exc)[:100])
        code = None
    if code == photo_check.NUDE:
        await _strike(db, uid, message, "На фото обнажёнка")
        return
    if code is not None:
        await message.answer(_PHOTO_HINTS.get(code, "Фото не подходит, пришли другое."))
        return
    await state.update_data(photo=photo)
    await state.set_state(NewPack.name)
    await message.answer("Как назвать пак/персонажа? (можно кириллицу и эмодзи)")


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
    """Store the human name (moderated), then ask adult/child."""
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
    await state.set_state(NewPack.subject)
    await message.answer("Это взрослый или ребёнок?", reply_markup=subject_kb())


async def on_subject(callback: CallbackQuery, state: FSMContext, loader: StyleLoader) -> None:
    """Branch on subject: ask age ONLY for a child (§B.4)."""
    tag_component("handlers.flow")
    subject = (callback.data or "").split(":", 1)[-1]
    if subject == "child":
        await state.update_data(subject="child")
        await state.set_state(NewPack.child_age)
        if isinstance(callback.message, Message):
            await callback.message.answer("Сколько лет ребёнку?", reply_markup=age_kb())
    else:
        await state.update_data(subject="adult", child_age=None)
        await state.set_state(NewPack.style)
        if isinstance(callback.message, Message):
            await callback.message.answer("Выбери стиль:", reply_markup=style_kb(loader))
    await callback.answer()


async def on_age(callback: CallbackQuery, state: FSMContext, loader: StyleLoader) -> None:
    """Store the child's age (0..18), then ask for a style."""
    tag_component("handlers.flow")
    age = int((callback.data or "age:0").split(":", 1)[-1])
    await state.update_data(child_age=age)
    await state.set_state(NewPack.style)
    if isinstance(callback.message, Message):
        await callback.message.answer("Выбери стиль:", reply_markup=style_kb(loader))
    await callback.answer()


# --- caption selection → create → preview → publish/download -----------------


async def _start_selection(msg: Message, state: FSMContext) -> None:  # pragma: no cover
    """Begin the standard-caption checklist (all selected by default)."""
    await state.update_data(std_sel=list(range(len(STANDARD_BLOCK))), custom=[], page=0)
    await state.set_state(NewPack.select_std)
    await msg.answer(
        "Выберите стандартные стикеры, которые надо включить в набор. Дальше "
        "сможете добавить свои, но всего не больше 15 (один лист).",
        reply_markup=std_checklist_kb(list(range(len(STANDARD_BLOCK))), 0),
    )


async def on_style(
    callback: CallbackQuery, state: FSMContext, db: Database
) -> None:  # pragma: no cover
    """Store the style and start caption selection (canonical is built on Create)."""
    tag_component("handlers.flow")
    style_id = (callback.data or "").split(":", 1)[-1]
    await state.update_data(mode="fresh", style_id=style_id)
    if callback.from_user is not None:
        await analytics.log(db, callback.from_user.id, analytics.STYLE_CHOSEN, style_id=style_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await _start_selection(callback.message, state)


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
    await state.set_state(NewPack.ask_custom)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Хотите добавить свои варианты?", reply_markup=ask_custom_kb()
        )


async def on_custom_yes(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    await state.set_state(NewPack.enter_custom)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите свой вариант:")


async def on_custom_no(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_review(callback.message, state)


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
    await _show_review(message, state)


async def _show_review(msg: Message, state: FSMContext) -> None:  # pragma: no cover
    data = await state.get_data()
    captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
    await state.set_state(NewPack.review)
    if not captions:
        await msg.answer(
            "Пока ничего не выбрано. Добавьте хотя бы один стикер.",
            reply_markup=review_kb(0),
        )
        return
    listing = "\n".join(f"{i}. {c}" for i, c in enumerate(captions, start=1))
    await msg.answer(
        f"Стикеры ({len(captions)}):\n{listing}",
        reply_markup=review_kb(len(captions)),
    )


async def on_rev_add(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    await state.set_state(NewPack.enter_custom)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите свой вариант:")


async def on_rev_remove(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    tag_component("handlers.flow")
    data = await state.get_data()
    captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
    kb = InlineKeyboardBuilder()
    for i, c in enumerate(captions):
        kb.button(text=f"➖ {c}", callback_data=f"rem:{i}")
    kb.adjust(2)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Какой убрать?", reply_markup=kb.as_markup())


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
    if isinstance(callback.message, Message):
        await _show_review(callback.message, state)


async def on_rev_create(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Generate the selected stickers (per page) and show a preview to publish/download."""
    tag_component("handlers.flow")
    data = await state.get_data()
    captions = selected_captions(data.get("std_sel", []), data.get("custom", []))
    user_id = callback.from_user.id if callback.from_user else 0
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    if not captions:
        await msg.answer("Выберите хотя бы один стикер.")
        return
    if (hint := await _generation_gate(db, user_id)) is not None:
        await msg.answer(hint)
        return

    std_names = [STANDARD_BLOCK[i] for i in sorted(set(data.get("std_sel", [])))]
    await analytics.log(
        db,
        user_id,
        analytics.CAPTIONS_SELECTED,
        standard=std_names,
        custom=list(data.get("custom", [])),
        total=len(captions),
    )
    started = time.monotonic()
    status = await msg.answer("🎨 Рисую персонажа…")

    async def on_step(done: int, total: int) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(
                f"🎨 Рисую персонажа… {_progress_bar(done, total)} {done}/{total}"
            )

    async def on_stage(label: str) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(_STAGE_TEXT.get(label, "Работаю…"))

    mode = data.get("mode", "fresh")
    try:
        async with _typing(msg):
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
        await analytics.log(
            db, user_id, analytics.GENERATION_ERROR, mode=mode, reason=str(exc)[:200]
        )
        await status.edit_text(_friendly_error(exc))
        if model_errors.is_quota(exc):
            await _alert_admins_quota(msg, exc)
        return

    await analytics.log(
        db,
        user_id,
        analytics.GENERATION_DONE,
        mode=mode,
        count=len(bundle.stickers),
        seconds=round(time.monotonic() - started, 1),
    )
    with contextlib.suppress(Exception):
        await status.delete()
    await _present_for_publish(
        msg, state, bundle.stickers, mode=mode, title=bundle.title, pack_id=bundle.pack_id
    )

    # Alpha: one generation consumed per delivered review; alert admins on low budget.
    if not _is_admin(user_id) and await modes.get_mode(db) == modes.ALPHA:
        await db.consume_generation(user_id)
    for alert in await budget.pending_alerts(db):
        for admin_id in get_settings().admin_id_list:
            with contextlib.suppress(Exception):
                await msg.bot.send_message(admin_id, alert)  # type: ignore[union-attr]


async def _present_for_publish(  # pragma: no cover
    msg: Message,
    state: FSMContext,
    stickers: list[StickerInput],
    *,
    mode: str,
    title: str,
    pack_id: int | None = None,
) -> None:
    """Send the transparent preview sheet(s) and offer publish/download."""
    await state.update_data(stickers=stickers, mode=mode, pack_id=pack_id, pub_title=title)
    await state.set_state(NewPack.publish)
    for i, sheet in enumerate(compose_preview([img for img, _ in stickers]), start=1):
        await msg.answer_document(BufferedInputFile(sheet, filename=f"preview_{i}.png"))
    await msg.answer(
        f"Готово: {len(stickers)} стикеров (прозрачный фон). Что дальше?",
        reply_markup=publish_kb(),
    )


async def on_publish_yes(  # pragma: no cover
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Publish the previewed stickers (new pack or extend)."""
    tag_component("handlers.flow")
    data = await state.get_data()
    user_id = callback.from_user.id if callback.from_user else 0
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    stickers: list[StickerInput] = data.get("stickers") or []
    if not stickers:
        await msg.answer("Нет готовых стикеров. Начни заново: /new")
        await state.clear()
        return

    status = await msg.answer("📦 Публикую пак в Telegram…")
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
        await status.edit_text(_friendly_error(exc))
        return

    event = analytics.EXTENDED if data.get("mode") == "extend" else analytics.PUBLISHED
    await analytics.log(db, user_id, event, set_name=result.set_name, count=result.count)
    await state.clear()
    await status.edit_text(f"✅ Готово! Пак: {result.link}")


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
        await callback.message.answer("Отменено. Новый пак: /new")


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Abort whatever the user is in the middle of (works from any FSM state)."""
    tag_component("handlers.flow")
    if await state.get_state() is None:
        await message.answer("Нечего отменять. Новый пак: /new")
        return
    await state.clear()
    await message.answer("Отменено. Начать заново: /new")


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
    kb = InlineKeyboardBuilder()
    for char in characters:
        kb.button(text=f"{char.name} ({char.style_id})", callback_data=f"char:{char.id}")
    kb.adjust(1)
    await message.answer("Новый пак про сохранённого персонажа:", reply_markup=kb.as_markup())


async def on_pick_char(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Reuse a saved character → caption selection → create."""
    tag_component("handlers.flow")
    char_id = int((callback.data or "char:0").split(":", 1)[-1])
    await state.update_data(mode="reuse", character_id=char_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await _start_selection(callback.message, state)


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
    kb = InlineKeyboardBuilder()
    for pack in packs:
        mark = "✅" if pack.published else "📝"
        kb.button(text=f"{mark} {pack.title}", callback_data=f"pk:{pack.id}")
    kb.adjust(1)
    await message.answer(
        "Ваши паки:\n\n🐞 Если что-то не так — напишите /report с подробностями.",
        reply_markup=kb.as_markup(),
    )


async def on_pick_saved_pack(  # pragma: no cover
    callback: CallbackQuery, db: Database
) -> None:
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
    kb.adjust(1)
    status = "опубликован" if pack.published else "черновик"
    await msg.answer(f"«{pack.title}» — {status}.", reply_markup=kb.as_markup())


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
        await msg.answer(f"Уже опубликован: {pack.link}")
        return
    stickers = await orchestrator.load_pack_stickers(pack_id)
    status = await msg.answer("📦 Публикую пак в Telegram…")
    try:
        async with _typing(msg):
            result = await orchestrator.publish_draft(
                owner_id=user_id, pack=pack, stickers=stickers
            )
    except Exception as exc:
        logger.exception("publish failed")
        await status.edit_text(_friendly_error(exc))
        return
    await analytics.log(
        db, user_id, analytics.PUBLISHED, set_name=result.set_name, count=result.count
    )
    await status.edit_text(f"✅ Готово! Пак: {result.link}")


async def on_pick_pack(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Extend an existing pack → caption selection → create → append."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "extend:0").split(":", 1)[-1])
    await state.update_data(mode="extend", pack_id=pack_id)
    await callback.answer()
    if isinstance(callback.message, Message):
        await _start_selection(callback.message, state)


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
    router.callback_query.register(on_rev_add, F.data == "rev:add")
    router.callback_query.register(on_rev_remove, F.data == "rev:remove")
    router.callback_query.register(on_rem_pick, F.data.startswith("rem:"))
    router.callback_query.register(on_publish_yes, F.data == "pub:yes")
    router.callback_query.register(on_pub_download, F.data == "pub:dl")
    router.callback_query.register(on_publish_no, F.data == "pub:no")
    router.callback_query.register(on_pick_char, F.data.startswith("char:"))
    router.callback_query.register(on_pick_pack, F.data.startswith("extend:"))
    router.callback_query.register(on_pick_saved_pack, F.data.startswith("pk:"))
    router.callback_query.register(on_saved_publish, F.data.startswith("pkpub:"))
    router.callback_query.register(on_saved_download, F.data.startswith("pkdl:"))
    return router
