"""Publish a pack to Telegram via Bot API (§9, §B.4).

Two branches: create a brand-new set, or add to an existing one (limit 120 per
set). The set **owner is the requesting user_id, never the bot** (§B.4). The
machine name is hidden; on the rare "name occupied" we regenerate the suffix and
retry once — no proactive availability check (§3.3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram.types import BufferedInputFile, InputSticker

from sticker_service.services.publish.naming import build_set_name, sticker_set_link

logger = logging.getLogger(__name__)

MAX_STICKERS_PER_SET = 120

# A (sticker_png_bytes, emoji) pair.
StickerInput = tuple[bytes, str]


class PackFullError(RuntimeError):
    """Adding would exceed Telegram's 120-stickers-per-set limit.

    Carries a ready, user-facing RU message (the flow shows ``str(exc)`` as-is),
    so the user is told how many slots remain *before* any paid work runs.
    """


def remaining_capacity(current_count: int) -> int:
    """Free slots left in a set before it hits the 120-sticker limit."""
    return max(0, MAX_STICKERS_PER_SET - current_count)


def capacity_error(title: str, current_count: int) -> PackFullError:
    """A ``PackFullError`` whose message tells the user how many slots remain."""
    room = remaining_capacity(current_count)
    if room == 0:
        return PackFullError(
            f"Пак «{title}» уже заполнен ({MAX_STICKERS_PER_SET}/{MAX_STICKERS_PER_SET}). "
            "Создай новый пак: /new"
        )
    return PackFullError(
        f"В паке «{title}» осталось {room} из {MAX_STICKERS_PER_SET} мест. "
        f"Выбери не больше {room} стикеров за раз или начни новый пак: /new"
    )


def _is_name_occupied(exc: Exception) -> bool:
    return "occupied" in str(exc).lower()


def _input_sticker(image: bytes, emoji: str, index: int) -> InputSticker:
    return InputSticker(
        sticker=BufferedInputFile(image, filename=f"sticker_{index}.png"),
        format="static",
        emoji_list=[emoji],
    )


class Publisher:
    """Thin Bot API wrapper for creating/extending sticker sets."""

    def __init__(self, bot: object, bot_username: str) -> None:
        self._bot = bot
        self._bot_username = bot_username

    @property
    def bot_username(self) -> str:
        return self._bot_username

    async def create_pack(
        self,
        *,
        user_id: int,
        title: str,
        stickers: Sequence[StickerInput],
        max_name_retries: int = 1,
    ) -> str:
        """Create a new set owned by ``user_id``; return its machine name."""
        input_stickers = [
            _input_sticker(image, emoji, i) for i, (image, emoji) in enumerate(stickers)
        ]
        attempts = max_name_retries + 1
        for attempt in range(attempts):
            name = build_set_name(title, self._bot_username)
            try:
                await self._bot.create_new_sticker_set(  # type: ignore[attr-defined]
                    user_id=user_id, name=name, title=title, stickers=input_stickers
                )
            except Exception as exc:  # narrow by message below, then re-raise
                if attempt < attempts - 1 and _is_name_occupied(exc):
                    logger.warning("set name %s occupied; regenerating suffix", name)
                    continue
                raise
            else:
                await self._set_cover(user_id, name, stickers)
                return name
        raise RuntimeError("unreachable: create_pack retry loop exhausted")  # pragma: no cover

    async def _set_cover(self, user_id: int, name: str, stickers: Sequence[StickerInput]) -> None:
        """Pick a random sticker, fit it to a cover, set it as the set thumbnail."""
        if not stickers:  # pragma: no cover - defensive
            return
        import secrets

        from sticker_service.services.postprocess import make_cover

        try:
            cover = make_cover(secrets.choice(list(stickers))[0])
            await self._bot.set_sticker_set_thumbnail(  # type: ignore[attr-defined]
                name=name,
                user_id=user_id,
                thumbnail=BufferedInputFile(cover, filename="cover.webp"),
                format="static",
            )
        except Exception as exc:  # cover is best-effort; never fail publish over it
            logger.warning("could not set set cover for %s: %s", name, str(exc)[:100])

    async def add_to_pack(
        self,
        *,
        user_id: int,
        set_name: str,
        stickers: Sequence[StickerInput],
        current_count: int,
    ) -> None:
        """Append stickers to an existing set, respecting the 120 limit."""
        if current_count + len(stickers) > MAX_STICKERS_PER_SET:
            raise capacity_error(set_name, current_count)
        for i, (image, emoji) in enumerate(stickers, start=current_count):
            await self._bot.add_sticker_to_set(  # type: ignore[attr-defined]
                user_id=user_id, name=set_name, sticker=_input_sticker(image, emoji, i)
            )

    @staticmethod
    def link(set_name: str) -> str:
        """Public add link for the set."""
        return sticker_set_link(set_name)
