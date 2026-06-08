"""``/report`` — bug report form for testers; forwarded to the first admin.

Collects a free-text description plus automatic context (reporter link, mode,
time) so the owner can follow up with the user.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.handlers.errors import _user_ref
from sticker_service.observability import tag_component
from sticker_service.services import modes


class Report(StatesGroup):
    text = State()


async def cmd_report(message: Message, state: FSMContext) -> None:
    """Start a bug report."""
    tag_component("handlers.report")
    await state.set_state(Report.text)
    await message.answer(
        "🐞 Опишите одним сообщением, что и где пошло не так — чем подробнее, тем "
        "лучше (что делали, что ожидали, что получили)."
    )


async def on_report_text(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    """Forward the report (with context + reporter link) to the first admin."""
    tag_component("handlers.report")
    text = (message.text or "").strip()
    await state.clear()
    admin = get_settings().first_admin_id
    if admin is not None and message.from_user is not None:
        mode = await db.get_config("mode", modes.DEFAULT)
        report = (
            "🐞 <b>Отчёт об ошибке</b>\n"
            f"Когда: {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC\n"
            f"От: {_user_ref(message.from_user)}\n"
            f"Режим: {modes.DISPLAY.get(mode, mode)}\n\n"
            f"{text}"
        )
        kb = InlineKeyboardBuilder()
        kb.button(
            text="✅ Баг подтверждён (+генерации)",
            callback_data=f"bug:{message.from_user.id}",
        )
        with contextlib.suppress(Exception):
            await bot.send_message(admin, report, parse_mode="HTML", reply_markup=kb.as_markup())
    await message.answer("Спасибо! Отчёт отправлен команде. 🙌")


def build_router() -> Router:
    """Build a fresh report router."""
    router = Router(name="report")
    router.message.register(cmd_report, Command("report"))
    router.message.register(on_report_text, Report.text)
    return router
