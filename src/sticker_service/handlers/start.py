"""``/start`` and ``/rules`` handlers (§3.1).

Thin presentation layer. Lives in an instrumented dir, so it tags its Sentry
component.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services import analytics, modes, pricing

# Chat types where the bot is a guest, not a personal workspace.
_GROUPISH = {ChatType.GROUP, ChatType.SUPERGROUP}

# Public greeting for groups/channels: never the private wizard — just wave and
# point everyone to the DM where packs are actually made.
GROUP_WELCOME = (
    "Привет! Я Юки — рисую персональные стикерпаки из одного фото 🎨\n"
    "Здесь, в чате, я просто машу ручкой 👋 Чтобы сделать свой пак — "
    "загляни ко мне в личку."
)

WELCOME = (
    "Привет! Я делаю персональные стикерпаки из фото: твой человек "
    "превращается в нарисованного персонажа с русскими подписями, "
    "и пак сразу публикуется в Telegram.\n\n"
    "Бот сейчас в закрытом тестировании.\n\n"
    "Нажми /new чтобы создать новый стикерпак, или /addto чтобы дополнить "
    "существующий — это вдвое дешевле.\n\n"
    "<i>Нажми /rules чтобы ознакомиться с правилами. Приступая к созданию "
    "стикеров, ты автоматически соглашаешься с ними.</i>"
)

HELP = (
    "🆘 <b>Что я умею</b>\n\n"
    "/new — новый стикерпак из фото\n"
    "/mychars — мои персонажи: новый пак про того же человека\n"
    "/mypacks — мои паки: открыть / опубликовать / скачать\n"
    "/addto — добавить стикеры в существующий пак\n"
    "/balance — остаток паков и цены\n"
    "/cancel — отменить текущее действие\n"
    "/rules — правила · /report — сообщить об ошибке\n\n"
    "<b>Как это работает:</b> пришли фото → выбери стиль → отметь подписи → "
    "посмотри превью → опубликуй пак в Telegram или скачай.\n\n"
    "💸 <b>В альфе у тебя бюджет в «паках»</b> (старт — 3):\n"
    "• новый пак — 1 пак\n"
    "• добавить стикеры к готовому персонажу — 0.5 пака\n"
    "Списание только после успешной генерации, ошибки — бесплатно.\n"
    "За подтверждённый баг из /report начисляем +2 пака."
)


async def alpha_balance_note(db: Database, user_id: int) -> str | None:
    """One-line balance reminder for alpha participants (None outside alpha/admins).

    The owner's rule: an alpha tester must always have the remaining budget in
    front of their eyes — so entry-point screens append this line.
    """
    if user_id in get_settings().admin_id_set:
        return None
    if await modes.get_mode(db) != modes.ALPHA:
        return None
    left = await db.credits_left(user_id)
    return f"💎 Баланс: {pricing.format_packs(left)} паков · цены и детали: /balance"


async def cmd_balance(message: Message, db: Database) -> None:
    """Show the remaining packs and the alpha price list."""
    tag_component("handlers.start")
    user = message.from_user
    if user is None:
        return
    if await modes.get_mode(db) != modes.ALPHA or user.id in get_settings().admin_id_set:
        await message.answer("Сейчас лимиты не действуют — твори свободно. Начни с /new")
        return
    left = await db.credits_left(user.id)
    await message.answer(
        f"💎 <b>Баланс: {pricing.format_packs(left)} паков</b>\n\n"
        "Цены в альфе:\n"
        f"• новый пак (/new) — {pricing.format_packs(pricing.COST_NEW_PACK)} пак\n"
        "• дополнить пак / новый пак про сохранённого персонажа "
        f"(/addto, /mychars) — {pricing.format_packs(pricing.COST_ADD_STICKERS)} пака\n"
        f"• перерисовать персонажа — {pricing.format_packs(pricing.COST_REDRAW)} пак\n\n"
        "Списание — только после успешной генерации; ошибки и отмены бесплатны.\n"
        "🐞 За каждый подтверждённый баг начисляем +2 пака: /report",
        parse_mode="HTML",
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


def _demo_button(kb: InlineKeyboardBuilder) -> None:
    """Append the showcase url-button when a demo page is configured.

    A button, not a text wall: every newcomer sees it on /start without the
    greeting getting any longer (owner's rule — visible, never pushy).
    """
    url = get_settings().demo_page_url
    if url:
        kb.button(text="✨ Примеры работ", url=url)


def _dm_keyboard() -> InlineKeyboardMarkup:
    """A single button that opens the bot's private chat."""
    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Хочу стикерпак!", url="https://t.me/yuki_stickers_bot")
    return kb.as_markup()


async def greet_group(message: Message, db: Database) -> None:  # pragma: no cover
    """Public greeting for a group/channel: wave + invite to the DM.

    Shared by ``/start`` in a group and by any mention/reply/name-address of the
    bot. Never runs the private application/wizard flow — that belongs in DMs.
    """
    tag_component("handlers.start")
    user = message.from_user
    if user is not None:
        with contextlib.suppress(Exception):
            await analytics.track_start(db, user.id)
    with contextlib.suppress(Exception):
        await message.answer(GROUP_WELCOME, reply_markup=_dm_keyboard())


async def _bot_addressed(message: Message) -> bool:
    """True when the bot is @mentioned, replied to, or called by name in a chat.

    @mention and reply work with Telegram privacy mode ON; name-addressing
    ("Юки, …") only reaches the bot when privacy mode is OFF in @BotFather, so
    we match it too and it simply never fires while privacy is on.
    """
    bot = message.bot
    if bot is None:  # pragma: no cover - always bound in production
        return False
    # Only real people address the bot; skip the channel's own auto-forwards
    # into the discussion group (sender_chat set, from_user empty).
    if message.from_user is None or message.from_user.is_bot:
        return False
    me = await bot.me()
    # Reply to one of the bot's own messages.
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.from_user.id == me.id:
        return True
    text = (message.text or message.caption or "").lower()
    if not text:
        return False
    handle = f"@{(me.username or '').lower()}"
    return handle in text or "юки" in text or "yuki" in text


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
        # The applicant can't try the bot yet — show what it makes meanwhile.
        _demo_button(kb)
        kb.adjust(1)
        await message.answer(
            "🤖 Бот сейчас в закрытом альфа-тестировании. Оставьте заявку на участие — "
            "и мы пригласим вас, как только появится место.",
            reply_markup=kb.as_markup(),
        )
        return
    text = WELCOME
    if (note := await alpha_balance_note(db, user.id)) is not None:
        text += f"\n\n{note}"
    kb = InlineKeyboardBuilder()
    if mode == modes.ALPHA and (welcome_url := get_settings().alpha_welcome_url):
        # Yuki's letter to testers: why the alpha matters + the bonus rules.
        kb.button(text="💛 Письмо от Юки", url=welcome_url)
    _demo_button(kb)
    kb.adjust(1)
    markup = kb.as_markup() if kb.as_markup().inline_keyboard else None
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


async def cmd_rules(message: Message) -> None:
    """Show the rules."""
    tag_component("handlers.start")
    await message.answer(RULES, parse_mode="HTML")


async def cmd_help(message: Message, db: Database) -> None:
    """Show what the bot can do and the alpha pricing (+ live balance)."""
    tag_component("handlers.start")
    text = HELP
    if url := get_settings().demo_page_url:
        text += f'\n\n✨ <a href="{url}">Примеры работ</a>'
    user = message.from_user
    if user is not None and (note := await alpha_balance_note(db, user.id)) is not None:
        text += f"\n\n{note}"
    await message.answer(text, parse_mode="HTML")


def build_router() -> Router:
    """Build a fresh start router (factory: safe to call per dispatcher)."""
    router = Router(name="start")
    # Groups/channels: /start and any mention/reply/name-address → public
    # greeting + DM invite. These are registered BEFORE the private handlers
    # and scoped to group chats, so the private wizard never leaks into a chat.
    router.message.register(greet_group, CommandStart(), F.chat.type.in_(_GROUPISH))
    router.message.register(greet_group, _bot_addressed, F.chat.type.in_(_GROUPISH))
    router.channel_post.register(greet_group, CommandStart())
    # Private chat: the personal flow.
    router.message.register(cmd_start, CommandStart(), F.chat.type == ChatType.PRIVATE)
    router.message.register(cmd_rules, Command("rules"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_balance, Command("balance"))
    return router
