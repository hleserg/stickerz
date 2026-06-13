"""Admin commands to manage the whitelist (§11.1).

``/allow <user_id>`` and ``/deny <user_id>`` — restricted to admin ids from
config, no role model. The durable key is the numeric ``user_id``; @username
resolution happens on first contact (post-MVP convenience).
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.db.repository import CREDITS_PER_PACK
from sticker_service.observability import tag_component
from sticker_service.services import (
    analytics,
    approvals,
    budget,
    charts,
    metrics,
    modes,
    pricing,
)
from sticker_service.services.approvals import BUG_BONUS_PACKS


class AdminFSM(StatesGroup):
    budget = State()  # awaiting alpha budget on mode switch


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_set


def _is_first_admin(user_id: int) -> bool:
    return user_id == get_settings().first_admin_id


async def _broadcast_admins(bot: Bot, text: str) -> None:
    """Send a message to every admin (e.g. budget alerts)."""
    for admin_id in get_settings().admin_id_list:
        with contextlib.suppress(Exception):
            await bot.send_message(admin_id, text)


def _parse_user_id(arg: str | None) -> int | None:
    if not arg:
        return None
    arg = arg.strip()
    return int(arg) if arg.lstrip("-").isdigit() else None


async def cmd_allow(message: Message, command: CommandObject, db: Database) -> None:
    """Add a user to the whitelist (admin only)."""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    user_id = _parse_user_id(command.args)
    if user_id is None:
        await message.answer("Использование: /allow <user_id> (числовой Telegram id)")
        return
    await db.allow(user_id)
    await message.answer(f"✅ Пользователь {user_id} добавлен в whitelist.")


async def cmd_deny(message: Message, command: CommandObject, db: Database) -> None:
    """Remove a user from the whitelist (admin only)."""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    user_id = _parse_user_id(command.args)
    if user_id is None:
        await message.answer("Использование: /deny <user_id>")
        return
    await db.deny(user_id)
    await message.answer(f"🚫 Пользователь {user_id} удалён из whitelist.")


async def cmd_stats(message: Message, db: Database) -> None:
    """Funnel infographic for the alpha (admin only).

    Admins' own events are excluded and the window opens when the alpha did
    (``modes.alpha_started_at``): friends who helped test before launch count
    as ordinary testers, but only for what they do DURING the alpha — so the
    funnel shows real tester activity, not anyone's dev/test runs.
    """
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    admins = get_settings().admin_id_list
    started = await modes.alpha_started_at(db)

    async def n(event: str) -> int:
        return await db.count_events(event, exclude_users=admins, since=started)

    items = [
        ("Запуски (/start)", await n(analytics.START)),
        ("Выбран стиль", await n(analytics.STYLE_CHOSEN)),
        ("Сгенерировано", await n(analytics.GENERATION_DONE)),
        ("Ошибки генерации", await n(analytics.GENERATION_ERROR)),
        ("Опубликовано", await n(analytics.PUBLISHED)),
        ("Дополнено", await n(analytics.EXTENDED)),
        ("Скачано", await n(analytics.DOWNLOADED)),
    ]
    png = await asyncio.to_thread(charts.render_bar_chart, "Воронка альфы", items)
    caption = f"{await metrics.alpha_metrics_text(db)}\n\n{await budget.summary_line(db)}"
    await message.answer_photo(BufferedInputFile(png, filename="stats.png"), caption=caption)


async def cmd_bans(message: Message, db: Database) -> None:
    """List active bans with unban buttons (admin only)."""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    bans = await db.list_bans()
    if not bans:
        await message.answer("Активных банов нет.")
        return
    kb = InlineKeyboardBuilder()
    for user_id, until in bans:
        kb.button(
            text=f"Снять бан {user_id} (до {until:%d.%m %H:%M})", callback_data=f"unban:{user_id}"
        )
    kb.adjust(1)
    await message.answer("Активные баны:", reply_markup=kb.as_markup())


async def on_unban(callback: CallbackQuery, db: Database) -> None:
    """Lift a ban (admin only)."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "unban:0").split(":", 1)[-1])
    await db.unban(user_id)
    await callback.answer("Бан снят")
    if isinstance(callback.message, Message):
        await callback.message.answer(f"✅ Бан с пользователя {user_id} снят.")


async def cmd_user(message: Message, command: CommandObject, db: Database) -> None:
    """Show a user's active strikes with reasons (admin only). /user <id>"""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    user_id = _parse_user_id(command.args)
    if user_id is None:
        await message.answer("Использование: /user <user_id>")
        return
    strikes = await db.list_strikes(user_id)
    until = await db.banned_until(user_id)
    lines = [f"Пользователь {user_id}", f"Активных страйков: {len(strikes)}"]
    if until is not None:
        lines.append(f"Забанен до: {until:%d.%m %H:%M}")
    for reason, created in strikes:
        lines.append(f"• {created[:16].replace('T', ' ')} — {reason}")
    await message.answer("\n".join(lines))


async def cmd_mode(message: Message, db: Database) -> None:
    """Show/switch the bot mode (FIRST admin only)."""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_first_admin(message.from_user.id):
        return
    current = await modes.get_mode(db)
    kb = InlineKeyboardBuilder()
    for mode in modes.MODES:
        mark = "▶️ " if mode == current else ""
        kb.button(text=f"{mark}{modes.DISPLAY[mode]}", callback_data=f"mode:{mode}")
    kb.adjust(1)
    await message.answer(
        f"Текущий режим: <b>{modes.DISPLAY.get(current, current)}</b>. Выберите новый:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


async def on_mode_pick(callback: CallbackQuery, db: Database) -> None:
    """Ask to confirm a mode switch (first admin only)."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_first_admin(callback.from_user.id):
        await callback.answer()
        return
    mode = (callback.data or "mode:").split(":", 1)[-1]
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    if not modes.is_implemented(mode):
        await callback.message.answer("Переключение невозможно: режим в разработке.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, точно", callback_data=f"modeyes:{mode}")
    kb.button(text="Отмена", callback_data="modeno")
    await callback.message.answer(
        f"Точно переключить в режим «{modes.DISPLAY[mode]}»?", reply_markup=kb.as_markup()
    )


async def on_mode_confirm(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    """Apply the confirmed mode switch (first admin only)."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_first_admin(callback.from_user.id):
        await callback.answer()
        return
    mode = (callback.data or "modeyes:").split(":", 1)[-1]
    await callback.answer()
    if not modes.is_implemented(mode):
        if isinstance(callback.message, Message):
            await callback.message.answer("Переключение невозможно: режим в разработке.")
        return
    if mode == modes.ALPHA:
        # Alpha needs a test budget first.
        await state.set_state(AdminFSM.budget)
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Укажите бюджет на альфа-тест (целое число долларов, ≥ 0):"
            )
        return
    await modes.set_mode(db, mode)
    if isinstance(callback.message, Message):
        await callback.message.answer(f"✅ Режим переключён: «{modes.DISPLAY[mode]}».")


async def on_budget_input(message: Message, state: FSMContext, db: Database) -> None:
    """Receive the alpha budget, then switch to alpha (first admin only)."""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_first_admin(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужно целое неотрицательное число долларов. Повторите:")
        return
    await budget.set_budget(db, int(raw))
    await modes.set_mode(db, modes.ALPHA)
    await state.clear()
    await message.answer(f"✅ Альфа-тест включён. Бюджет: ${int(raw)}.")


async def cmd_setbudget(message: Message, command: CommandObject, db: Database) -> None:
    """Set the alpha test budget (FIRST admin only). /setbudget <dollars>"""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_first_admin(message.from_user.id):
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: /setbudget <целое число долларов, ≥ 0>")
        return
    await budget.set_budget(db, int(raw))
    remaining = await budget.remaining_budget(db)
    await message.answer(f"✅ Бюджет: ${int(raw)}. Остаток сейчас: ${remaining:.2f}.")


async def cmd_gen(message: Message, command: CommandObject, db: Database) -> None:
    """Adjust a user's remaining packs (admin). /gen <user_id> <±N packs>"""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: /gen <user_id> <±N паков>")
        return
    user_id, delta_packs = int(parts[0]), int(parts[1])
    left = await db.add_credits(user_id, delta_packs * CREDITS_PER_PACK)
    await message.answer(f"Паков у {user_id}: {pricing.format_packs(left)}.")


_STATUS_CMD = {"waiting": "pending", "rejected": "rejected", "approved": "approved"}
_STATUS_TITLE = {"pending": "⏳ Ожидают", "rejected": "🚫 Отклонены", "approved": "✅ Одобрены"}


async def _show_applications(message: Message, db: Database, status: str) -> None:
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    apps = await db.list_applications(status)
    if not apps:
        await message.answer(f"{_STATUS_TITLE[status]}: пусто.")
        return
    kb = InlineKeyboardBuilder()
    for app in apps:
        handle = f"@{app.username}" if app.username else str(app.user_id)
        kb.button(text=f"{handle} — {app.source[:30]}", callback_data=f"appview:{app.user_id}")
    kb.adjust(1)
    await message.answer(_STATUS_TITLE[status], reply_markup=kb.as_markup())


async def cmd_waiting(message: Message, db: Database) -> None:
    tag_component("handlers.admin")
    await _show_applications(message, db, "pending")


async def cmd_rejected(message: Message, db: Database) -> None:
    tag_component("handlers.admin")
    await _show_applications(message, db, "rejected")


async def cmd_approved(message: Message, db: Database) -> None:
    tag_component("handlers.admin")
    await _show_applications(message, db, "approved")


async def on_app_view(callback: CallbackQuery, db: Database) -> None:
    """Show one application with approve/reject/open-chat actions."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "appview:0").split(":", 1)[-1])
    app = await db.get_application(user_id)
    await callback.answer()
    if app is None or not isinstance(callback.message, Message):
        return
    handle = f"@{app.username}" if app.username else "—"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"appok:{user_id}")
    kb.button(text="🚫 Отклонить", callback_data=f"appno:{user_id}")
    kb.button(text="💬 Открыть чат", url=f"tg://user?id={user_id}")
    kb.adjust(2)
    await callback.message.answer(
        f"Заявка {handle} (id={user_id})\nИсточник: {app.source}\n"
        f"Дата: {app.created_at:%d.%m %H:%M}\nСтатус: {app.status}",
        reply_markup=kb.as_markup(),
    )


async def on_app_approve(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Approve: whitelist + grant default packs + welcome the user."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "appok:0").split(":", 1)[-1])
    await approvals.approve_user(db, user_id)
    await callback.answer("Одобрено")
    with contextlib.suppress(Exception):
        await bot.send_message(user_id, approvals.welcome_text())
    if isinstance(callback.message, Message):
        await callback.message.answer(f"✅ {user_id} одобрен и уведомлён.")


async def on_app_reject(callback: CallbackQuery, db: Database) -> None:
    """Reject silently (no user notification)."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "appno:0").split(":", 1)[-1])
    await db.set_application_status(user_id, "rejected")
    await callback.answer("Отклонено")
    if isinstance(callback.message, Message):
        await callback.message.answer(f"🚫 {user_id} отклонён (без уведомления).")


async def on_bug_confirm(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Confirm a report as a real bug → grant bonus packs."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "bug:0").split(":", 1)[-1])
    left = await db.add_credits(user_id, BUG_BONUS_PACKS * CREDITS_PER_PACK)
    await callback.answer("Засчитано")
    with contextlib.suppress(Exception):
        await bot.send_message(
            user_id, f"🐞 Спасибо за найденный баг! Начислили +{BUG_BONUS_PACKS} пака."
        )
    if isinstance(callback.message, Message):
        total = pricing.format_packs(left)
        await callback.message.answer(
            f"✅ +{BUG_BONUS_PACKS} пака пользователю {user_id} (итого {total})."
        )


# Owner-approved apology (2026-06-12) sent with the refunded charge.
REFUND_USER_TEXT = (
    "😔 Прости за бракованный пак — такое не должно доходить до тебя. "
    "Вернули {amount} пак. на баланс: можешь перерисовать бесплатно — "
    "/new или /addto. Спасибо, что рассказал!"
)


_REFUND_NOTHING = "Возвращать нечего: списаний нет или последнее уже возвращено."

# user_ids with a refund mid-flight: a double-tap on a laggy connection must
# not run two concurrent refunds for the same user.
_refunds_in_flight: set[int] = set()


async def _unrefunded_charge(db: Database, user_id: int) -> tuple[datetime, int, str] | None:
    """The user's latest real charge as (when, credits, mode), or None.

    None when the user was never charged, the event is malformed, or the
    charge is older than the latest refund (= already settled). This is the
    single gate that keeps a refund from gifting credits on top.
    """
    charges = await db.events_for(user_id, analytics.CREDITS_CHARGED, limit=1)
    if not charges:
        return None
    when, detail = charges[0]
    raw = detail.get("credits")
    credits = raw if isinstance(raw, int) else 0
    if credits <= 0:
        return None
    refunds = await db.events_for(user_id, analytics.CREDITS_REFUNDED, limit=1)
    if refunds and refunds[0][0] >= when:
        return None
    mode = detail.get("mode")
    return when, credits, mode if isinstance(mode, str) else "?"


async def _strip_card_keyboard(callback: CallbackQuery) -> None:
    """Best-effort disarm of the pressed card so a stale button can't re-fire."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)


async def on_refund_request(callback: CallbackQuery, db: Database) -> None:
    """Show the user's latest unrefunded charge before refunding.

    The 🐞 card arrives for any report (hangs, strikes…), so the refund must
    verify a charge actually happened — never gift credits on top — and let
    the admin see what exactly is being returned before confirming.
    """
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "refund:0").split(":", 1)[-1])
    charge = await _unrefunded_charge(db, user_id)
    if charge is None:
        await callback.answer(_REFUND_NOTHING, show_alert=True)
        return
    when, credits, mode = charge
    kb = InlineKeyboardBuilder()
    # The payload carries only the user: the amount is re-verified against the
    # latest unrefunded charge at confirm time, so a stale card can't replay.
    kb.button(text="✅ Вернуть", callback_data=f"refundok:{user_id}")
    kb.button(text="✖️ Отмена", callback_data="refundno")
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"💝 Последнее списание {user_id}: {pricing.format_packs(credits)} пак., "
            f"{when:%d.%m %H:%M} UTC, режим {mode}. Вернуть?",
            reply_markup=kb.as_markup(),
        )


async def on_refund_confirm(callback: CallbackQuery, db: Database, bot: Bot) -> None:
    """Re-verify the charge, mark it refunded, return it and apologize.

    The settled-marker and the credit are written in ONE transaction
    (``refund_credits``): neither a stranded refund (marker without credit,
    which the dedup gate would then block forever) nor a double-pay (credit
    without marker) is reachable.
    """
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "refundok:0").split(":", 1)[-1])
    if user_id in _refunds_in_flight:
        await callback.answer("Уже выполняю…")
        return
    _refunds_in_flight.add(user_id)
    try:
        charge = await _unrefunded_charge(db, user_id)
        if charge is None:  # double-tap, stale card or a repeat 🐞 report
            await callback.answer(_REFUND_NOTHING, show_alert=True)
            await _strip_card_keyboard(callback)
            return
        _, credits, _ = charge
        left = await db.refund_credits(
            user_id, credits, analytics.CREDITS_REFUNDED, {"credits": credits}
        )
    finally:
        _refunds_in_flight.discard(user_id)
    await callback.answer("Возвращено")
    await _strip_card_keyboard(callback)
    with contextlib.suppress(Exception):
        await bot.send_message(
            user_id, REFUND_USER_TEXT.format(amount=pricing.format_packs(credits))
        )
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"💝 Вернули {pricing.format_packs(credits)} пак. юзеру {user_id} "
            f"(итого {pricing.format_packs(left)})."
        )


async def on_refund_cancel(callback: CallbackQuery) -> None:
    tag_component("handlers.admin")
    await callback.answer("Отменено")


async def on_mode_cancel(callback: CallbackQuery) -> None:
    tag_component("handlers.admin")
    await callback.answer("Отменено")


# --- user management by buttons (no typing ids) ------------------------------

_USERS_PER_PAGE = 8


def _pack_balance_text(credits: int) -> str:
    return f"💎 {pricing.format_packs(credits)} пак."


async def _users_keyboard(db: Database, page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Build the paginated whitelist list; each user is a button to their card."""
    entries = await db.list_whitelist()
    if not entries:
        return None
    pages = (len(entries) + _USERS_PER_PAGE - 1) // _USERS_PER_PAGE
    page = max(0, min(page, pages - 1))
    chunk = entries[page * _USERS_PER_PAGE : (page + 1) * _USERS_PER_PAGE]
    kb = InlineKeyboardBuilder()
    for e in chunk:
        handle = f"@{e.username}" if e.username else f"id {e.user_id}"
        credits = await db.credits_left(e.user_id)
        kb.button(
            text=f"{handle} · {pricing.format_packs(credits)} пак.",
            callback_data=f"uc:{e.user_id}",
        )
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("◀ Назад", f"users:{page - 1}"))
    if page < pages - 1:
        nav.append(("Вперёд ▶", f"users:{page + 1}"))
    for text, data in nav:
        kb.button(text=text, callback_data=data)
    # one user per row, the nav buttons share the last row
    rows = [1] * len(chunk) + ([len(nav)] if nav else [])
    kb.adjust(*rows)
    return (
        f"👥 Пользователи (стр. {page + 1}/{pages}). Выбери — откроется карточка:",
        kb.as_markup(),
    )


async def _user_card(db: Database, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Render one user's card: status + action buttons."""
    allowed = await db.is_allowed(user_id)
    credits = await db.credits_left(user_id)
    until = await db.banned_until(user_id)
    entry = next((e for e in await db.list_whitelist() if e.user_id == user_id), None)
    handle = f"@{entry.username}" if entry and entry.username else "—"
    lines = [
        f"👤 {handle} (id {user_id})",
        _pack_balance_text(credits),
        f"Доступ: {'✅ есть' if allowed else '🚫 нет'}",
    ]
    if until is not None:
        lines.append(f"🚫 Бан до {until.astimezone():%d.%m %H:%M}")
    kb = InlineKeyboardBuilder()
    if allowed:
        kb.button(text="🚫 Убрать доступ", callback_data=f"uct:{user_id}")
    else:
        kb.button(text="✅ Дать доступ", callback_data=f"uct:{user_id}")
    kb.button(text="➕ +1 пак", callback_data=f"ucg:{user_id}:{CREDITS_PER_PACK}")
    kb.button(text="➖ −1 пак", callback_data=f"ucg:{user_id}:{-CREDITS_PER_PACK}")
    kb.button(text="💬 Открыть чат", url=f"tg://user?id={user_id}")
    kb.button(text="⬅️ К списку", callback_data="users:0")
    kb.adjust(1, 2, 1, 1)
    return ("\n".join(lines), kb.as_markup())


async def cmd_users(message: Message, db: Database) -> None:
    """List whitelisted users as buttons — tap to manage (no id typing)."""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    view = await _users_keyboard(db, 0)
    if view is None:
        await message.answer("В whitelist пока никого. Одобри заявки: /waiting")
        return
    text, markup = view
    await message.answer(text, reply_markup=markup)


async def on_users_page(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Paginate the user list in place."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    page = int((callback.data or "users:0").split(":", 1)[-1])
    await callback.answer()
    view = await _users_keyboard(db, page)
    if view is not None and isinstance(callback.message, Message):
        text, markup = view
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup)


async def on_user_card(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Open a user's card from the list."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "uc:0").split(":", 1)[-1])
    await callback.answer()
    if isinstance(callback.message, Message):
        text, markup = await _user_card(db, user_id)
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup)


async def on_user_toggle(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Toggle a user's whitelist access from the card, then refresh it."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    user_id = int((callback.data or "uct:0").split(":", 1)[-1])
    if await db.is_allowed(user_id):
        await db.deny(user_id)
        await callback.answer("Доступ убран")
    else:
        await db.allow(user_id)
        await callback.answer("Доступ выдан")
    if isinstance(callback.message, Message):
        text, markup = await _user_card(db, user_id)
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup)


async def on_user_gen(callback: CallbackQuery, db: Database) -> None:  # pragma: no cover
    """Adjust a user's pack balance by ±1 pack from the card, then refresh it."""
    tag_component("handlers.admin")
    if callback.from_user is None or not _is_admin(callback.from_user.id):
        await callback.answer()
        return
    _, raw_id, raw_delta = (callback.data or "ucg:0:0").split(":")
    user_id, delta = int(raw_id), int(raw_delta)
    left = await db.add_credits(user_id, delta)
    await callback.answer(f"Баланс: {pricing.format_packs(left)} пак.")
    if isinstance(callback.message, Message):
        text, markup = await _user_card(db, user_id)
        with contextlib.suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup)


def build_router() -> Router:
    """Build a fresh admin router (factory: safe to call per dispatcher)."""
    router = Router(name="admin")
    router.message.register(cmd_users, Command("users"))
    router.message.register(cmd_allow, Command("allow"))
    router.message.register(cmd_deny, Command("deny"))
    router.message.register(cmd_stats, Command("stats"))
    router.message.register(cmd_bans, Command("bans"))
    router.message.register(cmd_user, Command("user"))
    router.message.register(cmd_mode, Command("mode"))
    router.message.register(cmd_setbudget, Command("setbudget"))
    router.message.register(cmd_gen, Command("gen"))
    router.message.register(cmd_waiting, Command("waiting"))
    router.message.register(cmd_rejected, Command("rejected"))
    router.message.register(cmd_approved, Command("approved"))
    router.message.register(on_budget_input, AdminFSM.budget)
    router.callback_query.register(on_unban, F.data.startswith("unban:"))
    router.callback_query.register(on_mode_pick, F.data.startswith("mode:"))
    router.callback_query.register(on_mode_confirm, F.data.startswith("modeyes:"))
    router.callback_query.register(on_mode_cancel, F.data == "modeno")
    router.callback_query.register(on_app_view, F.data.startswith("appview:"))
    router.callback_query.register(on_app_approve, F.data.startswith("appok:"))
    router.callback_query.register(on_app_reject, F.data.startswith("appno:"))
    router.callback_query.register(on_bug_confirm, F.data.startswith("bug:"))
    router.callback_query.register(on_refund_request, F.data.startswith("refund:"))
    router.callback_query.register(on_refund_confirm, F.data.startswith("refundok:"))
    router.callback_query.register(on_refund_cancel, F.data == "refundno")
    router.callback_query.register(on_users_page, F.data.startswith("users:"))
    router.callback_query.register(on_user_card, F.data.startswith("uc:"))
    router.callback_query.register(on_user_toggle, F.data.startswith("uct:"))
    router.callback_query.register(on_user_gen, F.data.startswith("ucg:"))
    return router
