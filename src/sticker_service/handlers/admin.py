"""Admin commands to manage the whitelist (§11.1).

``/allow <user_id>`` and ``/deny <user_id>`` — restricted to admin ids from
config, no role model. The durable key is the numeric ``user_id``; @username
resolution happens on first contact (post-MVP convenience).
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.observability import tag_component


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_id_set


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


def build_router() -> Router:
    """Build a fresh admin router (factory: safe to call per dispatcher)."""
    router = Router(name="admin")
    router.message.register(cmd_allow, Command("allow"))
    router.message.register(cmd_deny, Command("deny"))
    return router
