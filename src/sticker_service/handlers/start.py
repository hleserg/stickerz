"""``/start`` and ``/rules`` handlers (§3.1).

Thin presentation layer. Lives in an instrumented dir, so it tags its Sentry
component.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services import analytics, modes

WELCOME = (
    "Привет! Я делаю персональные стикерпаки из фото: твой человек "
    "превращается в нарисованного персонажа с русскими подписями, "
    "и пак сразу публикуется в Telegram.\n\n"
    "Бот сейчас в закрытом тестировании.\n\n"
    "Нажмите /new чтобы создать новый стикерпак.\n\n"
    "<i>Нажмите /rules чтобы ознакомиться с правилами. Приступая к созданию "
    "стикеров вы автоматически соглашаетесь с ними.</i>"
)

RULES = (
    "📜 <b>Правила</b>\n\n"
    "Запрещено и будет отклонено автоматически:\n"
    "• обнажёнка и любой эротический/непристойный контент;\n"
    "• мат и оскорбления в подписях;\n"
    "• насилие, кровь, жестокость.\n\n"
    "Фото: один человек, без обнажёнки, лицо достаточно крупное.\n\n"
    "За каждое нарушение начисляется страйк. 10 страйков — блокировка на 2 часа, "
    "15 — на сутки, 30 — на месяц. Страйки сгорают через месяц.\n\n"
    "Если система заблокировала вас по ошибке или вы нашли другие баги — напишите "
    "@skhlebnikov или используйте /report.\n\n"
    "<i>Бот в бета-тестировании — будем рады любой обратной связи по ошибкам, чтобы "
    "поскорее их исправить.</i>\n\n"
    "Приступая к созданию стикеров, вы соглашаетесь с этими правилами."
)


async def cmd_start(message: Message, db: Database) -> None:
    """Greet the user; in alpha, gate behind an application."""
    tag_component("handlers.start")
    user = message.from_user
    if user is None:
        await message.answer(WELCOME, parse_mode="HTML")
        return
    await analytics.track_start(db, user.id)

    mode = await modes.get_mode(db)
    is_admin = user.id in get_settings().admin_id_set
    approved = await db.is_allowed(user.id)
    if mode == modes.ALPHA and not is_admin and not approved:
        app = await db.get_application(user.id)
        if app is not None and app.status == "pending":
            await message.answer("⏳ Ваша заявка на участие в тесте на рассмотрении. Спасибо!")
            return
        if app is not None and app.status == "rejected":
            await message.answer("К сожалению, заявка на участие отклонена. Спасибо за интерес!")
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="📝 Оставить заявку", callback_data="apply:new")
        await message.answer(
            "🤖 Бот сейчас в закрытом альфа-тестировании. Оставьте заявку на участие — "
            "и мы пригласим вас, как только появится место.",
            reply_markup=kb.as_markup(),
        )
        return
    await message.answer(WELCOME, parse_mode="HTML")


async def cmd_rules(message: Message) -> None:
    """Show the rules."""
    tag_component("handlers.start")
    await message.answer(RULES, parse_mode="HTML")


def build_router() -> Router:
    """Build a fresh start router (factory: safe to call per dispatcher)."""
    router = Router(name="start")
    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_rules, Command("rules"))
    return router
