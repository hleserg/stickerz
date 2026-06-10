"""Test-participation application flow (alpha): leave a request + source."""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services import approvals


class Apply(StatesGroup):
    source = State()


async def on_apply(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask where the user heard about the bot."""
    tag_component("handlers.apply")
    await state.set_state(Apply.source)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Откуда вы узнали о боте? Напишите пару слов (до 300 символов)."
        )


async def on_apply_source(message: Message, state: FSMContext, db: Database) -> None:
    """Store the application; the first N testers get in automatically.

    While the alpha has open auto seats (APP_ALPHA_AUTO_APPROVE_LIMIT), the
    application is approved on the spot: the user gets the same welcome an
    admin-approved tester would, and admins get a heads-up instead of a chore.
    """
    tag_component("handlers.apply")
    source = (message.text or "").strip()
    if not source:
        await message.answer("Напишите пару слов текстом, пожалуйста.")
        return
    await state.clear()
    user = message.from_user
    if user is not None:
        await db.add_application(user.id, user.username, source[:300])
        if await approvals.maybe_auto_approve(db, user.id):
            await message.answer(approvals.welcome_text())
            await _notify_admins_auto_approved(message, db, user.id, user.username)
            return
    await message.answer("✅ Заявка отправлена! Мы пригласим вас, как только появится место. 🙌")


async def _notify_admins_auto_approved(
    message: Message, db: Database, user_id: int, username: str | None
) -> None:
    """Tell the admins a seat was taken automatically (best-effort)."""
    settings = get_settings()
    taken = len(await db.list_applications("approved"))
    handle = f"@{username}" if username else f"id={user_id}"
    text = (
        f"🤖 Автоодобрен тестер {handle} (id={user_id}) — "
        f"занято {taken}/{settings.alpha_auto_approve_limit} автомест."
    )
    for admin_id in settings.admin_id_list:
        with contextlib.suppress(Exception):
            await message.bot.send_message(admin_id, text)  # type: ignore[union-attr]


def build_router() -> Router:
    """Build a fresh apply router."""
    router = Router(name="apply")
    router.callback_query.register(on_apply, F.data == "apply:new")
    # Commands (/cancel, /start, …) fall through to their real handlers instead
    # of being swallowed as the application's "source" text.
    router.message.register(on_apply_source, Apply.source, ~F.text.startswith("/"))
    return router
