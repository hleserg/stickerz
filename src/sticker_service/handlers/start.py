"""``/start`` entry handler (§3.1).

Thin presentation layer: real action selection (new pack / add to pack) is
wired in later tasks. Lives in an instrumented dir, so it tags its Sentry
component.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from sticker_service.observability import tag_component

WELCOME = (
    "Привет! Я делаю персональные стикерпаки из фото: твой человек "
    "превращается в нарисованного персонажа с русскими подписями, "
    "и пак сразу публикуется в Telegram.\n\n"
    "Бот сейчас в закрытом тестировании.\n\n"
    "Нажмите /new чтобы создать новый стикерпак."
)


async def cmd_start(message: Message) -> None:
    """Greet the user and outline what the bot does."""
    tag_component("handlers.start")
    await message.answer(WELCOME)


def build_router() -> Router:
    """Build a fresh start router (factory: safe to call per dispatcher)."""
    router = Router(name="start")
    router.message.register(cmd_start, CommandStart())
    return router
