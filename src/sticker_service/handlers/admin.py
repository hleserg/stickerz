"""Admin commands to manage the whitelist (§11.1).

``/allow <user_id>`` and ``/deny <user_id>`` — restricted to admin ids from
config, no role model. The durable key is the numeric ``user_id``; @username
resolution happens on first contact (post-MVP convenience).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component
from sticker_service.services import analytics, charts, modes


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_set


def _is_first_admin(user_id: int) -> bool:
    return user_id == get_settings().first_admin_id


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
    await message.answer_photo(BufferedInputFile(png, filename="stats.png"))


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


async def on_mode_confirm(callback: CallbackQuery, db: Database) -> None:
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
    await modes.set_mode(db, mode)
    if isinstance(callback.message, Message):
        await callback.message.answer(f"✅ Режим переключён: «{modes.DISPLAY[mode]}».")


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
    router.callback_query.register(on_unban, F.data.startswith("unban:"))
    router.callback_query.register(on_mode_pick, F.data.startswith("mode:"))
    router.callback_query.register(on_mode_confirm, F.data.startswith("modeyes:"))
    router.callback_query.register(on_mode_cancel, F.data == "modeno")
    return router
