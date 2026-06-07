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

router = Router(name="start")

WELCOME = (
    "Привет! Я делаю персональные стикерпаки из фото: твой человек "
    "превращается в нарисованного персонажа с русскими подписями, "
    "и пак сразу публикуется в Telegram.\n\n"
    "Бот сейчас в закрытом тестировании."
)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Greet the user and outline what the bot does."""
    tag_component("handlers.start")
    await message.answer(WELCOME)
