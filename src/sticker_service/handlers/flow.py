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

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class NewPack(StatesGroup):
    """FSM states for building a brand-new pack with a new character."""

    consent = State()
    photo = State()
    name = State()
    subject = State()
    child_age = State()
    style = State()
    confirm = State()


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


def confirm_kb() -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Похож, генерируем!", callback_data="canon:ok")
    kb.button(text="🔁 Переснять", callback_data="canon:retake")
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
    """Run the canonical pipeline and show it for confirmation (§4)."""
    tag_component("handlers.flow")
    style_id = (callback.data or "").split(":", 1)[-1]
    data = await state.get_data()
    await state.update_data(style_id=style_id)
    if isinstance(callback.message, Message):
        await callback.message.answer("Генерирую персонажа, это займёт минуту…")
    canonical = await orchestrator.build_canonical(
        photo=data["photo"],
        style_id=style_id,
        subject_type=data["subject"],
        child_age=data.get("child_age"),
    )
    await state.update_data(canonical=canonical)
    await state.set_state(NewPack.confirm)
    if isinstance(callback.message, Message):
        await callback.message.answer("Похож? Генерируем стикеры?", reply_markup=confirm_kb())
    await callback.answer()


async def on_confirm(  # pragma: no cover - publishes via Bot API
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator
) -> None:
    """Save the character and build+publish the pack (§3.2)."""
    tag_component("handlers.flow")
    data = await state.get_data()
    user_id = callback.from_user.id if callback.from_user else 0
    character = await orchestrator.save_character(
        owner_id=user_id,
        name=data["name"],
        style_id=data["style_id"],
        subject_type=data["subject"],
        child_age=data.get("child_age"),
        canonical=data["canonical"],
    )
    result = await orchestrator.create_pack(owner_id=user_id, character=character)
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(f"Готово! Пак: {result.link}")
    await callback.answer()


def build_router() -> Router:
    """Build a fresh flow router (factory: safe to call per dispatcher)."""
    router = Router(name="flow")
    router.message.register(cmd_new, Command("new"))
    router.callback_query.register(on_consent, F.data == "consent:yes")
    router.message.register(on_photo, NewPack.photo)
    router.message.register(on_name, NewPack.name)
    router.callback_query.register(on_subject, F.data.startswith("subject:"))
    router.callback_query.register(on_age, F.data.startswith("age:"))
    router.callback_query.register(on_style, F.data.startswith("style:"))
    router.callback_query.register(on_confirm, F.data == "canon:ok")
    router.callback_query.register(on_style, F.data == "canon:retake")
    return router
