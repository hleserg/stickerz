"""Test-participation application flow (alpha): leave a request + source."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sticker_service.db import Database
from sticker_service.observability import tag_component


class Apply(StatesGroup):
    source = State()


async def on_apply(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask where the user heard about the bot."""
    tag_component("handlers.apply")
    await state.set_state(Apply.source)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Откуда вы узнали о боте? Напишите пару слов.")


async def on_apply_source(message: Message, state: FSMContext, db: Database) -> None:
    """Store the application (link + source + date + pending status)."""
    tag_component("handlers.apply")
    source = (message.text or "").strip()[:300] or "—"
    await state.clear()
    user = message.from_user
    if user is not None:
        await db.add_application(user.id, user.username, source)
    await message.answer("✅ Заявка отправлена! Мы пригласим вас, как только появится место. 🙌")


def build_router() -> Router:
    """Build a fresh apply router."""
    router = Router(name="apply")
    router.callback_query.register(on_apply, F.data == "apply:new")
    router.message.register(on_apply_source, Apply.source)
    return router
