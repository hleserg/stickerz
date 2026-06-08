"""Admin commands to manage the whitelist (§11.1).

``/allow <user_id>`` and ``/deny <user_id>`` — restricted to admin ids from
config, no role model. The durable key is the numeric ``user_id``; @username
resolution happens on first contact (post-MVP convenience).
"""

from __future__ import annotations

import contextlib

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.db.repository import CREDITS_PER_PACK, DEFAULT_CREDITS
from sticker_service.observability import tag_component
from sticker_service.services import analytics, budget, charts, modes, pricing

BUG_BONUS_PACKS = 2  # extra packs for a confirmed bug report


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
    """Funnel infographic (admin only)."""
    tag_component("handlers.admin")
    if message.from_user is None or not _is_admin(message.from_user.id):
        return
    items = [
        ("Запуски (/start)", await db.count_events(analytics.START)),
        ("Выбран стиль", await db.count_events(analytics.STYLE_CHOSEN)),
        ("Сгенерировано", await db.count_events(analytics.GENERATION_DONE)),
        ("Ошибки генерации", await db.count_events(analytics.GENERATION_ERROR)),
        ("Опубликовано", await db.count_events(analytics.PUBLISHED)),
        ("Дополнено", await db.count_events(analytics.EXTENDED)),
        ("Скачано", await db.count_events(analytics.DOWNLOADED)),
    ]
    png = charts.render_bar_chart("Воронка Stickerz", items)
    await message.answer_photo(
        BufferedInputFile(png, filename="stats.png"), caption=await budget.summary_line(db)
    )


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
    await db.set_application_status(user_id, "approved")
    await db.allow(user_id)
    await db.set_credits(user_id, DEFAULT_CREDITS)
    await callback.answer("Одобрено")
    packs = pricing.format_packs(DEFAULT_CREDITS)
    with contextlib.suppress(Exception):
        await bot.send_message(
            user_id,
            f"🎉 Рады приветствовать вас в тестировании! Вам доступно "
            f"{packs} бесплатных паков (новый пак — 1, добавить стикеры — 0.5). "
            f"За каждый подтверждённый баг из /report начислим ещё. "
            f"Что умеет бот — /help. Поехали: /new",
        )
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


async def on_mode_cancel(callback: CallbackQuery) -> None:
    tag_component("handlers.admin")
    await callback.answer("Отменено")


def build_router() -> Router:
    """Build a fresh admin router (factory: safe to call per dispatcher)."""
    router = Router(name="admin")
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
    return router
