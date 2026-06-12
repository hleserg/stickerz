"""``/report`` — bug report form for testers; forwarded to the first admin.

Collects a free-text description plus automatic context (reporter link, mode,
time) so the owner can follow up with the user.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.handlers.errors import _user_ref
from sticker_service.observability import tag_component
from sticker_service.services import modes


class Report(StatesGroup):
    text = State()


REPORT_PROMPT = (
    "🐞 <b>Сообщить об ошибке</b>\n\n"
    "Баг-репорт — это рассказ о том, что бот сделал не так: кривой стикер, "
    "артефакты на фоне, зависание, непонятное сообщение, несправедливый страйк.\n\n"
    "Напишите одним сообщением: что делали → что ожидали → что получилось. "
    "Чем подробнее, тем быстрее починим.\n\n"
    "💎 За каждый подтверждённый баг начисляем <b>+2 пака</b> к балансу.\n\n"
    "Передумали? /cancel"
)

REPORT_THANKS = (
    "✅ Спасибо! Отчёт ушёл команде. Если подтвердим баг — начислим бонусные паки и напишем вам."
)


async def cmd_report(message: Message, state: FSMContext) -> None:
    """Start a bug report."""
    tag_component("handlers.report")
    await state.set_state(Report.text)
    await message.answer(REPORT_PROMPT, parse_mode="HTML")


async def on_report_text(message: Message, state: FSMContext, db: Database, bot: Bot) -> None:
    """Forward the report (with context + reporter link) to the first admin."""
    tag_component("handlers.report")
    text = (message.text or message.caption or "").strip()
    if not text:
        await message.answer("Опишите проблему текстом, пожалуйста. Или /cancel.")
        return
    await state.clear()
    admin = get_settings().first_admin_id
    if admin is not None and message.from_user is not None:
        mode = await db.get_config("mode", modes.DEFAULT)
        # User text is escaped: it lands in a parse_mode="HTML" message, where a
        # stray <>& would be either markup injection or a rejected send.
        report = (
            "🐞 <b>Отчёт об ошибке</b>\n"
            f"Когда: {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC\n"
            f"От: {_user_ref(message.from_user)}\n"
            f"Режим: {modes.DISPLAY.get(mode, mode)}\n\n"
            f"{escape(text)}"
        )
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        kb = InlineKeyboardBuilder()
        kb.button(
            text="✅ Баг подтверждён (+генерации)",
            callback_data=f"bug:{message.from_user.id}",
        )
        # Separate from the bug bonus: a delivered defective pack deserves its
        # charge back even when the bug itself is already known (owner's rule).
        kb.button(
            text="💝 Вернуть списанный пак",
            callback_data=f"refund:{message.from_user.id}",
        )
        kb.adjust(1)
        with contextlib.suppress(Exception):
            await bot.send_message(admin, report, parse_mode="HTML", reply_markup=kb.as_markup())
    await message.answer(REPORT_THANKS)


def build_router() -> Router:
    """Build a fresh report router."""
    router = Router(name="report")
    router.message.register(cmd_report, Command("report"))
    # Commands fall through to their real handlers (so /cancel cancels instead
    # of being forwarded to the admin as a "bug report").
    router.message.register(on_report_text, Report.text, ~F.text.startswith("/"))
    return router
