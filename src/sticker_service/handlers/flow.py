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
from sticker_service.services.orchestrator import Orchestrator
from sticker_service.services.postprocess import compose_preview
from sticker_service.services.publish.publisher import StickerInput

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
    publish = State()  # stickers generated, preview shown, awaiting publish/cancel


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


def publish_kb() -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Опубликовать в Telegram", callback_data="pub:yes")
    kb.button(text="❌ Отмена", callback_data="pub:no")
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


async def on_photo(message: Message, state: FSMContext) -> None:  # pragma: no cover - Telegram IO
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
    """Store the human name, then ask adult/child."""
    tag_component("handlers.flow")
    await state.update_data(name=(message.text or "").strip() or "Мой пак")
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


async def on_style(  # pragma: no cover - runs the model pipeline
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator
) -> None:
    """Build canonical, generate stickers (no 'похож' confirm), show a preview."""
    tag_component("handlers.flow")
    style_id = (callback.data or "").split(":", 1)[-1]
    data = await state.get_data()
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return

    status = await msg.answer("🎨 Рисую персонажа… (шаг 1)")

    async def on_step(done: int, total: int) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(f"🎨 Рисую персонажа… шаг {done}/{total}")

    async def on_stage(label: str) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(_STAGE_TEXT.get(label, "Работаю…"))

    user_id = callback.from_user.id if callback.from_user else 0
    try:
        async with _typing(msg):
            canonical = await orchestrator.build_canonical(
                photo=data["photo"],
                style_id=style_id,
                subject_type=data["subject"],
                child_age=data.get("child_age"),
                on_step=on_step,
            )
            character = await orchestrator.save_character(
                owner_id=user_id,
                name=data["name"],
                style_id=style_id,
                subject_type=data["subject"],
                child_age=data.get("child_age"),
                canonical=canonical,
            )
            stickers = await orchestrator.build_stickers(character, on_stage=on_stage)
    except Exception as exc:
        logger.exception("generation failed")
        await status.edit_text(_friendly_error(exc))
        await state.clear()
        return

    with contextlib.suppress(Exception):
        await status.delete()
    await _present_for_publish(
        msg, state, stickers, mode="new", character_id=character.id, title=character.name
    )


async def _present_for_publish(  # pragma: no cover - Telegram IO
    msg: Message,
    state: FSMContext,
    stickers: list[StickerInput],
    *,
    mode: str,
    character_id: int,
    title: str,
    pack_id: int | None = None,
) -> None:
    """Send the transparent preview sheet(s) and ask whether to publish."""
    await state.update_data(
        stickers=stickers, mode=mode, character_id=character_id, pack_id=pack_id, pub_title=title
    )
    await state.set_state(NewPack.publish)
    for i, sheet in enumerate(compose_preview([img for img, _ in stickers]), start=1):
        await msg.answer_document(BufferedInputFile(sheet, filename=f"preview_{i}.png"))
    await msg.answer(
        f"Готово: {len(stickers)} стикеров (прозрачный фон). Публикуем пак в Telegram?",
        reply_markup=publish_kb(),
    )


async def on_publish_yes(  # pragma: no cover - publishes via Bot API
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database
) -> None:
    """Publish the previously-generated stickers (new pack or extend)."""
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


async def on_publish_no(
    callback: CallbackQuery, state: FSMContext
) -> None:  # pragma: no cover - IO
    """Cancel before publishing."""
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


async def on_pick_char(  # pragma: no cover - runs the model + Bot API
    callback: CallbackQuery, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:
    """Reuse a saved character to generate stickers, then preview before publish (§3.2)."""
    tag_component("handlers.flow")
    char_id = int((callback.data or "char:0").split(":", 1)[-1])
    character = await db.get_character(char_id)
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    if character is None:
        await msg.answer("Персонаж не найден")
        return
    status = await msg.answer("✨ Генерирую стикеры…")

    async def on_stage(label: str) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(_STAGE_TEXT.get(label, "Работаю…"))

    try:
        async with _typing(msg):
            stickers = await orchestrator.build_stickers(character, on_stage=on_stage)
    except Exception as exc:
        logger.exception("generation failed")
        await status.edit_text(_friendly_error(exc))
        return
    with contextlib.suppress(Exception):
        await status.delete()
    await _present_for_publish(
        msg, state, stickers, mode="new", character_id=character.id, title=character.name
    )


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


async def on_pick_pack(  # pragma: no cover - runs the model + Bot API
    callback: CallbackQuery, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:
    """Generate stickers for the pack's character, preview, then append on confirm (§3.2)."""
    tag_component("handlers.flow")
    pack_id = int((callback.data or "extend:0").split(":", 1)[-1])
    pack = await db.get_pack(pack_id)
    await callback.answer()
    msg = callback.message if isinstance(callback.message, Message) else None
    if msg is None:
        return
    if pack is None:
        await msg.answer("Пак не найден")
        return
    character = await db.get_character(pack.character_id)
    if character is None:
        await msg.answer("Персонаж пака не найден")
        return
    status = await msg.answer("✨ Генерирую стикеры…")

    async def on_stage(label: str) -> None:
        with contextlib.suppress(Exception):
            await status.edit_text(_STAGE_TEXT.get(label, "Работаю…"))

    try:
        async with _typing(msg):
            stickers = await orchestrator.build_stickers(character, on_stage=on_stage)
    except Exception as exc:
        logger.exception("generation failed")
        await status.edit_text(_friendly_error(exc))
        return
    with contextlib.suppress(Exception):
        await status.delete()
    await _present_for_publish(
        msg,
        state,
        stickers,
        mode="extend",
        character_id=character.id,
        title=pack.title,
        pack_id=pack.id,
    )


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
    router.callback_query.register(on_publish_yes, F.data == "pub:yes")
    router.callback_query.register(on_publish_no, F.data == "pub:no")
    router.callback_query.register(on_pick_char, F.data.startswith("char:"))
    router.callback_query.register(on_pick_pack, F.data.startswith("extend:"))
    return router
