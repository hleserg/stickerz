"""Publish a pack to Telegram via Bot API (§9, §B.4).

Two branches: create a brand-new set, or add to an existing one (limit 120 per
set). The set **owner is the requesting user_id, never the bot** (§B.4). The
machine name is hidden; on the rare "name occupied" we regenerate the suffix and
retry once — no proactive availability check (§3.3).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputSticker

from sticker_service.services.publish.naming import build_set_name, sticker_set_link
from sticker_service.services.stickers.emoji import DEFAULT_EMOJI

logger = logging.getLogger(__name__)

MAX_STICKERS_PER_SET = 120
# How many RetryAfter sleeps to tolerate per API call before giving up.
_FLOOD_RETRIES = 3
# Telegram's retry_after is uncapped (can announce minutes-hours under abuse
# heuristics). Sleeping that out would pin the user's single-flight slot and a
# polling task slot — longer waits are re-raised so the user gets the friendly
# error now and can simply retry later (the resume marker makes that safe).
_FLOOD_WAIT_MAX_S = 60

# A (sticker_png_bytes, emoji) pair.
StickerInput = tuple[bytes, str]

# Awaited after each sticker lands in the set (position, sticker) — lets the
# caller persist progress so a crash mid-batch never desyncs DB from Telegram.
AddedCallback = Callable[[int, StickerInput], Awaitable[None]]


async def _flood_wait[T](call: Callable[[], Awaitable[T]], *, retries: int = _FLOOD_RETRIES) -> T:
    """Await ``call``, sleeping out short Telegram RetryAfter waits.

    Re-raises immediately when the announced wait exceeds ``_FLOOD_WAIT_MAX_S``
    or after ``retries`` sleeps — a pathological 429 must become a visible
    error, not a multi-hour silent hang.
    """
    for _attempt in range(retries):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            if exc.retry_after > _FLOOD_WAIT_MAX_S:
                logger.warning("telegram flood wait %ss exceeds cap — giving up", exc.retry_after)
                raise
            logger.warning("telegram flood wait: sleeping %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
    return await call()


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


def _is_bad_emoji(exc: Exception) -> bool:
    # "Bad Request: can't parse sticker: expected a Unicode emoji" (live 13.06):
    # Telegram's emoji set is the only authority, and it is stricter than any
    # upstream validation we run — so the publisher keeps a fallback of its own.
    return "unicode emoji" in str(exc).lower()


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
        name_retries = 0
        emoji_fallback_used = False
        while True:
            name = build_set_name(title, self._bot_username)
            try:
                await _flood_wait(
                    lambda name=name, input_stickers=input_stickers: (
                        self._bot.create_new_sticker_set(  # type: ignore[attr-defined]
                            user_id=user_id, name=name, title=title, stickers=input_stickers
                        )
                    )
                )
            except Exception as exc:  # narrow by message below, then re-raise
                if name_retries < max_name_retries and _is_name_occupied(exc):
                    name_retries += 1
                    logger.warning("set name %s occupied; regenerating suffix", name)
                    continue
                if not emoji_fallback_used and _is_bad_emoji(exc):
                    # The create call is atomic, so Telegram won't say WHICH
                    # emoji it disliked — retry the batch on safe defaults
                    # rather than lose the user's paid generation.
                    emoji_fallback_used = True
                    logger.warning("telegram rejected an emoji in «%s»; retrying defaults", title)
                    input_stickers = [
                        _input_sticker(image, DEFAULT_EMOJI, i)
                        for i, (image, _emoji) in enumerate(stickers)
                    ]
                    continue
                raise
            await self._set_cover(user_id, name, stickers)
            return name

    async def _set_cover(self, user_id: int, name: str, stickers: Sequence[StickerInput]) -> None:
        """Pick a random sticker, fit it to a cover, set it as the set thumbnail."""
        if not stickers:  # pragma: no cover - defensive
            return
        import secrets

        from sticker_service.services.postprocess import make_cover

        try:
            # No flood-wait here on purpose: the cover is decorative, and the
            # user is waiting for the publish result — a 429 falls straight
            # into the best-effort except instead of stalling completion.
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
        on_added: AddedCallback | None = None,
    ) -> None:
        """Append stickers to an existing set, respecting the 120 limit.

        ``on_added`` (if given) is awaited after each sticker actually lands,
        so the caller can persist progress — a crash mid-batch then leaves the
        DB at most one sticker behind the Telegram set, and the orchestrator's
        resume marker can retry the same batch without duplicates.
        """
        if current_count + len(stickers) > MAX_STICKERS_PER_SET:
            raise capacity_error(set_name, current_count)
        for i, (image, emoji) in enumerate(stickers, start=current_count):
            try:
                await _flood_wait(
                    lambda i=i, image=image, emoji=emoji: self._bot.add_sticker_to_set(  # type: ignore[attr-defined]
                        user_id=user_id, name=set_name, sticker=_input_sticker(image, emoji, i)
                    )
                )
            except Exception as exc:
                if not _is_bad_emoji(exc):
                    raise
                # Land the sticker on the safe default instead of failing the
                # batch; on_added below persists the emoji Telegram really has.
                logger.warning("telegram rejected emoji %r; retrying with default", emoji)
                emoji = DEFAULT_EMOJI
                await _flood_wait(
                    lambda i=i, image=image: self._bot.add_sticker_to_set(  # type: ignore[attr-defined]
                        user_id=user_id,
                        name=set_name,
                        sticker=_input_sticker(image, DEFAULT_EMOJI, i),
                    )
                )
            if on_added is not None:
                await on_added(i, (image, emoji))

    async def live_count(self, set_name: str) -> int | None:
        """The set's actual sticker count per Telegram; None when the bot can't tell.

        RetryAfter (and other API errors) propagate: deciding whether to resume
        or skip MUST NOT silently degrade under flood — a wrong guess here is
        exactly what duplicates or drops stickers.
        """
        getter = getattr(self._bot, "get_sticker_set", None)
        if getter is None:  # test doubles / providers without the method
            return None
        sticker_set = await _flood_wait(lambda: getter(name=set_name))
        return len(sticker_set.stickers)

    @staticmethod
    def link(set_name: str) -> str:
        """Public add link for the set."""
        return sticker_set_link(set_name)
