"""Conversational pack-building flow (§3.1): new/existing character, extend.

This is the aiogram I/O shell that drives the user through consent → photo →
params → style → canonical → confirm → published pack, and the "add to pack"
branch. Business logic lives in the tested service layer (orchestrator, engine,
postprocess); this module wires Telegram interaction to it.

Invariants enforced here and covered by tests:
- consent (fact + timestamp) is recorded BEFORE a photo is accepted (§15.2);
- age is asked ONLY for children, never for adults (§B.4 / {age_clause}).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.moderation import caption_rejection_reason
from sticker_service.services.orchestrator import Orchestrator
from sticker_service.services.postprocess import bundle_zip, compose_preview
from sticker_service.services.publish.publisher import StickerInput
from sticker_service.services.stickers import STANDARD_BLOCK, selected_captions

logger = logging.getLogger(__name__)

_STAGE_TEXT = {
    "sheet": "✨ Рисую лист стикеров…",
    "slice": "✂️ Нарезаю на отдельные стикеры…",
    "emoji": "🎭 Подбираю эмодзи…",
    "publish": "📦 Публикую пак в Telegram…",
}


def _friendly_error(exc: Exception) -> str:
    """Turn a backend exception into a short user-facing message."""
    s = str(exc).lower()
    if "503" in s or "unavailable" in s or "overload" in s or "high demand" in s:
        return "⚠️ Модель сейчас перегружена (503). Попробуй ещё раз через минуту."
    if "refus" in s or "safety" in s or "prohibited" in s:
        return "⚠️ Модель отклонила генерацию (фильтр). Попробуй другое фото или возраст."
    if any(w in s for w in ("proxy", "connect", "timeout", "resolve", "ssl", "network")):
        return "⚠️ Нет доступа к модели (сеть/прокси). Проверь APP_MODELS_PROXY_URL и логи."
    return f"⚠️ Не получилось: {exc}"


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

    consent = State()
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


def consent_kb() -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтверждаю", callback_data="consent:yes")
    return kb.as_markup()


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


async def cmd_new(message: Message, state: FSMContext) -> None:
    """Start a new pack: ask for photo-rights consent first (§15.2)."""
    tag_component("handlers.flow")
    await state.clear()
    await state.set_state(NewPack.consent)
    await message.answer(
        "Создаём новый пак. Подтверди: у тебя есть право на это фото и согласие "
        "изображённого человека.",
        reply_markup=consent_kb(),
    )


async def on_consent(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    """Record consent (fact + timestamp) and only then ask for the photo."""
    tag_component("handlers.flow")
    if callback.from_user is not None:
        await db.record_consent(callback.from_user.id)
    await state.set_state(NewPack.photo)
    if isinstance(callback.message, Message):
        await callback.message.answer("Пришли фото человека.")
    await callback.answer()


async def on_photo(message: Message, state: FSMContext) -> None:  # pragma: no cover
    """Accept the photo (only valid after consent), then ask for a name."""
    tag_component("handlers.flow")
    if not message.photo:
        await message.answer("Нужно именно фото. Пришли изображение человека.")
        return
    bot = message.bot
    file = await bot.download(message.photo[-1].file_id)  # type: ignore[union-attr]
    await state.update_data(photo=file.read() if file else b"")
    await state.set_state(NewPack.name)
    await message.answer("Как назвать пак/персонажа? (можно кириллицу и эмодзи)")


async def on_name(message: Message, state: FSMContext) -> None:
    """Store the human name (moderated), then ask adult/child."""
    tag_component("handlers.flow")
    name = (message.text or "").strip()
    reason = caption_rejection_reason(name)
    if reason:
        await message.answer(f"⚠️ Так назвать нельзя ({reason}). Введите другое имя.")
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
        "сможете добавить свои, но всего не больше 24.",
        reply_markup=std_checklist_kb(list(range(len(STANDARD_BLOCK))), 0),
    )


async def on_style(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """Store the style and start caption selection (canonical is built on Create)."""
    tag_component("handlers.flow")
    style_id = (callback.data or "").split(":", 1)[-1]
    await state.update_data(mode="fresh", style_id=style_id)
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


async def on_enter_custom(message: Message, state: FSMContext) -> None:  # pragma: no cover
    """Append a typed custom caption (capped at 24), then show the review list."""
    tag_component("handlers.flow")
    text = (message.text or "").strip()
    reason = caption_rejection_reason(text)
    if reason:
        await message.answer(f"⚠️ Так нельзя ({reason}). Введите другой вариант.")
        return
    data = await state.get_data()
    custom = list(data.get("custom", []))
    total = len(selected_captions(data.get("std_sel", []), custom))
    if text and total < 24:
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

    status = await msg.answer("🎨 Рисую персонажа…")

    async def on_step(done: int, total: int) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(f"🎨 Рисую персонажа… шаг {done}/{total}")

    async def on_stage(label: str) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(_STAGE_TEXT.get(label, "Работаю…"))

    mode = data.get("mode", "fresh")
    try:
        async with _typing(msg):
            if mode == "extend":
                pack = await db.get_pack(int(data["pack_id"]))
                if pack is None:
                    raise RuntimeError("pack not found")
                character = await db.get_character(pack.character_id)
                title, pack_id = pack.title, pack.id
            elif mode == "reuse":
                character = await db.get_character(int(data["character_id"]))
                title, pack_id = (character.name if character else ""), None
            else:  # fresh — build canonical now
                canonical = await orchestrator.build_canonical(
                    photo=data["photo"],
                    style_id=data["style_id"],
                    subject_type=data["subject"],
                    child_age=data.get("child_age"),
                    on_step=on_step,
                )
                character = await orchestrator.save_character(
                    owner_id=user_id,
                    name=data["name"],
                    style_id=data["style_id"],
                    subject_type=data["subject"],
                    child_age=data.get("child_age"),
                    canonical=canonical,
                )
                title, pack_id = character.name, None
            if character is None:
                raise RuntimeError("character not found")
            stickers = await orchestrator.build_stickers(
                character, captions=captions, on_stage=on_stage
            )
    except Exception as exc:
        logger.exception("generation failed")
        await status.edit_text(_friendly_error(exc))
        return

    with contextlib.suppress(Exception):
        await status.delete()
    await _present_for_publish(
        msg, state, stickers, mode=mode, character_id=character.id, title=title, pack_id=pack_id
    )


async def _present_for_publish(  # pragma: no cover
    msg: Message,
    state: FSMContext,
    stickers: list[StickerInput],
    *,
    mode: str,
    character_id: int,
    title: str,
    pack_id: int | None = None,
) -> None:
    """Send the transparent preview sheet(s) and offer publish/download."""
    await state.update_data(
        stickers=stickers, mode=mode, character_id=character_id, pack_id=pack_id, pub_title=title
    )
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
            if data.get("mode") == "extend":
                pack = await db.get_pack(int(data["pack_id"]))
                if pack is None:
                    raise RuntimeError("pack not found")
                result = await orchestrator.publish_extend(
                    owner_id=user_id, pack=pack, stickers=stickers
                )
            else:
                character = await db.get_character(int(data["character_id"]))
                if character is None:
                    raise RuntimeError("character not found")
                result = await orchestrator.publish_new(
                    owner_id=user_id,
                    character=character,
                    stickers=stickers,
                    title=data.get("pub_title"),
                )
    except Exception as exc:
        logger.exception("publish failed")
        await status.edit_text(_friendly_error(exc))
        return

    await state.clear()
    await status.edit_text(f"✅ Готово! Пак: {result.link}")


async def on_pub_download(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
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


async def cmd_mychars(message: Message, db: Database) -> None:
    """List saved characters; selecting one starts a new pack about them (§3.2)."""
    tag_component("handlers.flow")
    user_id = message.from_user.id if message.from_user else 0
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
    """List the user's packs; selecting one adds stickers to that same set (§3.2)."""
    tag_component("handlers.flow")
    user_id = message.from_user.id if message.from_user else 0
    packs = await db.list_packs(user_id)
    if not packs:
        await message.answer("Пока нет паков для дополнения. Создай пак: /new")
        return
    kb = InlineKeyboardBuilder()
    for pack in packs:
        kb.button(text=pack.title, callback_data=f"extend:{pack.id}")
    kb.adjust(1)
    await message.answer(
        "Дополнить пак (тем же персонажем — новое фото нельзя, иначе пак станет разнородным):",
        reply_markup=kb.as_markup(),
    )


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
    router.message.register(cmd_mychars, Command("mychars"))
    router.message.register(cmd_addto, Command("addto"))
    router.callback_query.register(on_consent, F.data == "consent:yes")
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
    return router
