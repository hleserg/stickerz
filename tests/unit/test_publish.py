"""Tests for set-name generation and the publisher (mocked Bot API)."""

from __future__ import annotations

import random
import re

import pytest

from sticker_service.services.publish import (
    MAX_STICKERS_PER_SET,
    PackFullError,
    Publisher,
    build_set_name,
    capacity_error,
    remaining_capacity,
    sticker_set_link,
    transliterate,
)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*_by_yourbot$")


# --- naming ------------------------------------------------------------------


def test_transliterate_russian() -> None:
    assert transliterate("Лёшик") == "leshik"
    assert transliterate("Привет 123!") == "privet123"


def test_build_set_name_shape() -> None:
    name = build_set_name("Лёшик 🎨", "YourBot", rng=random.Random(1))
    assert _NAME_RE.match(name)
    assert name.startswith("leshik_")
    assert name.endswith("_by_yourbot")


def test_build_set_name_prefixes_letter_when_needed() -> None:
    # Title with no latin/cyrillic start -> still begins with a letter.
    name = build_set_name("123", "Bot", rng=random.Random(0))
    assert name[0].isalpha()
    assert _NAME_RE.sub("", name) == name or name.endswith("_by_bot")


def test_sticker_set_link() -> None:
    assert sticker_set_link("foo_by_bot") == "https://t.me/addstickers/foo_by_bot"


# --- publisher ---------------------------------------------------------------


class _FakeBot:
    def __init__(self, *, occupied_first: bool = False) -> None:
        self.created: list[dict[str, object]] = []
        self.added: list[dict[str, object]] = []
        self.thumbnails: list[str] = []
        self._occupied_first = occupied_first

    async def create_new_sticker_set(self, **kwargs: object) -> None:
        if self._occupied_first and not self.created:
            self.created.append({"failed": kwargs["name"]})
            raise RuntimeError("Bad Request: sticker set name is already occupied")
        self.created.append(kwargs)

    async def add_sticker_to_set(self, **kwargs: object) -> None:
        self.added.append(kwargs)

    async def set_sticker_set_thumbnail(self, **kwargs: object) -> None:
        self.thumbnails.append(str(kwargs["name"]))


def _real_png() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGBA", (200, 240), (10, 120, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_create_pack_owner_is_user() -> None:
    bot = _FakeBot()
    pub = Publisher(bot, "yourbot")
    name = await pub.create_pack(user_id=42, title="Лёшик", stickers=[(b"\x89PNG", "🙂")])
    assert _NAME_RE.match(name)
    success = bot.created[-1]
    assert success["user_id"] == 42  # owner = user, not bot (§B.4)
    assert success["name"] == name


async def test_create_pack_sets_cover() -> None:
    bot = _FakeBot()
    pub = Publisher(bot, "yourbot")
    name = await pub.create_pack(user_id=42, title="Лёшик", stickers=[(_real_png(), "🙂")])
    assert bot.thumbnails == [name]  # a cover thumbnail was set for the new set


async def test_create_pack_retries_on_occupied() -> None:
    bot = _FakeBot(occupied_first=True)
    pub = Publisher(bot, "yourbot")
    name = await pub.create_pack(user_id=1, title="Test", stickers=[(b"x", "🙂")])
    # One failed attempt recorded, then a success with a different name.
    assert len(bot.created) == 2
    assert "failed" in bot.created[0]
    assert bot.created[1]["name"] == name


async def test_create_pack_reraises_non_occupied() -> None:
    class _Boom(_FakeBot):
        async def create_new_sticker_set(self, **kwargs: object) -> None:
            raise RuntimeError("Bad Request: PEER_ID_INVALID")

    pub = Publisher(_Boom(), "yourbot")
    with pytest.raises(RuntimeError, match="PEER_ID_INVALID"):
        await pub.create_pack(user_id=1, title="T", stickers=[(b"x", "🙂")])


async def test_add_to_pack_appends() -> None:
    bot = _FakeBot()
    pub = Publisher(bot, "yourbot")
    await pub.add_to_pack(
        user_id=7, set_name="s_by_yourbot", stickers=[(b"a", "🙂"), (b"b", "👍")], current_count=3
    )
    assert len(bot.added) == 2
    assert all(call["user_id"] == 7 for call in bot.added)


# --- flood waits (Telegram 429 must neither crash publish nor hang forever) ---


def _retry_after(seconds: int = 0) -> Exception:
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import GetMe

    return TelegramRetryAfter(method=GetMe(), message="flood", retry_after=seconds)


class _FloodingBot(_FakeBot):
    """Rejects the first N calls of each method with RetryAfter, then succeeds."""

    def __init__(self, *, floods: int) -> None:
        super().__init__()
        self._floods: dict[str, int] = {"create": floods, "add": floods}

    async def create_new_sticker_set(self, **kwargs: object) -> None:
        if self._floods["create"] > 0:
            self._floods["create"] -= 1
            raise _retry_after()
        await super().create_new_sticker_set(**kwargs)

    async def add_sticker_to_set(self, **kwargs: object) -> None:
        if self._floods["add"] > 0:
            self._floods["add"] -= 1
            raise _retry_after()
        await super().add_sticker_to_set(**kwargs)


async def test_create_pack_sleeps_out_flood_waits() -> None:
    bot = _FloodingBot(floods=2)
    pub = Publisher(bot, "yourbot")
    name = await pub.create_pack(user_id=1, title="T", stickers=[(b"x", "🙂")])
    assert bot.created[-1]["name"] == name  # succeeded after waiting twice


async def test_create_pack_gives_up_after_too_many_flood_waits() -> None:
    from aiogram.exceptions import TelegramRetryAfter

    bot = _FloodingBot(floods=99)
    pub = Publisher(bot, "yourbot")
    with pytest.raises(TelegramRetryAfter):
        await pub.create_pack(user_id=1, title="T", stickers=[(b"x", "🙂")])


async def test_add_to_pack_retries_each_sticker_through_flood_waits() -> None:
    bot = _FloodingBot(floods=2)
    pub = Publisher(bot, "yourbot")
    await pub.add_to_pack(
        user_id=7, set_name="s_by_yourbot", stickers=[(b"a", "🙂"), (b"b", "👍")], current_count=0
    )
    assert len(bot.added) == 2  # both landed despite the 429s


async def test_flood_wait_rejects_pathological_retry_after_immediately() -> None:
    """A multi-minute RetryAfter must become a visible error now, not a silent
    hang that pins the user's single-flight slot and a polling task slot."""
    from aiogram.exceptions import TelegramRetryAfter

    class _LongFlood(_FakeBot):
        async def create_new_sticker_set(self, **kwargs: object) -> None:
            raise _retry_after(3600)

    pub = Publisher(_LongFlood(), "yourbot")
    import asyncio

    with pytest.raises(TelegramRetryAfter):
        # Far under 3600 s: proves we re-raised instead of sleeping it out.
        await asyncio.wait_for(
            pub.create_pack(user_id=1, title="T", stickers=[(b"x", "🙂")]), timeout=2
        )


async def test_add_to_pack_reports_each_landed_sticker() -> None:
    """on_added fires per landed sticker with its set position — the
    orchestrator persists rows incrementally so the DB never lags the set."""
    bot = _FakeBot()
    pub = Publisher(bot, "yourbot")
    landed: list[tuple[int, bytes]] = []

    async def on_added(position: int, sticker: tuple[bytes, str]) -> None:
        landed.append((position, sticker[0]))

    await pub.add_to_pack(
        user_id=7,
        set_name="s_by_yourbot",
        stickers=[(b"a", "🙂"), (b"b", "👍")],
        current_count=3,
        on_added=on_added,
    )
    assert landed == [(3, b"a"), (4, b"b")]


async def test_live_count_reads_telegram_and_handles_missing_method() -> None:
    from types import SimpleNamespace

    class _WithGetter(_FakeBot):
        async def get_sticker_set(self, *, name: str) -> object:
            return SimpleNamespace(stickers=[object()] * 7)

    assert await Publisher(_WithGetter(), "b").live_count("s") == 7
    assert await Publisher(_FakeBot(), "b").live_count("s") is None  # no method → no resume


async def test_add_to_pack_enforces_limit() -> None:
    pub = Publisher(_FakeBot(), "yourbot")
    with pytest.raises(PackFullError):
        await pub.add_to_pack(
            user_id=1,
            set_name="s_by_yourbot",
            stickers=[(b"a", "🙂")],
            current_count=MAX_STICKERS_PER_SET,
        )


def test_remaining_capacity() -> None:
    assert remaining_capacity(0) == MAX_STICKERS_PER_SET
    assert remaining_capacity(MAX_STICKERS_PER_SET - 10) == 10
    assert remaining_capacity(MAX_STICKERS_PER_SET) == 0
    assert remaining_capacity(MAX_STICKERS_PER_SET + 5) == 0  # never negative


def test_capacity_error_message_states_remaining_slots() -> None:
    # Some room left: the message names how many slots remain.
    exc = capacity_error("Котик", MAX_STICKERS_PER_SET - 3)
    assert isinstance(exc, PackFullError)
    assert "3" in str(exc)
    assert "Котик" in str(exc)
    # Full: a distinct "create a new pack" message.
    full = capacity_error("Котик", MAX_STICKERS_PER_SET)
    assert "заполнен" in str(full)
