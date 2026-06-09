"""Tests for the weekly meme-pool trend refresh (validation-first, never breaks)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sticker_service.db import Database
from sticker_service.services.models import MockImageModel
from sticker_service.services.models.base import ImageModel
from sticker_service.services.stickers.meme_pool import (
    POOL_REFRESHED_KEY,
    active_pool,
)
from sticker_service.services.stickers.refresh_memes import (
    build_refresh_prompt,
    meme_refresh_loop,
    pool_is_stale,
    refresh_meme_pool,
    strip_code_fences,
)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def _pool_json(n: int, *, profane_first: int = 0) -> str:
    items = []
    for i in range(n):
        bad = " говно" if i < profane_first else ""
        items.append({"category": "тренды", "description": f"новая идея {i}{bad}", "caption": None})
    return json.dumps({"items": items}, ensure_ascii=False)


async def test_refresh_stores_validated_pool_and_stamps_time(db: Database) -> None:
    model = MockImageModel(text_response=_pool_json(60))
    assert await refresh_meme_pool(model, db) is True
    pool = await active_pool(db)
    assert len(pool) == 60
    assert pool[0].description.startswith("новая идея")
    assert await db.get_config(POOL_REFRESHED_KEY) != ""
    # The instruction carries the audience slant, the format and the current pool.
    prompt = model.text_calls[0]
    assert "60/40" in prompt
    assert "JSON" in prompt
    assert "Я проснулась. Технически." in prompt  # bundled pool sent for rewriting


async def test_refresh_accepts_markdown_fenced_json(db: Database) -> None:
    fenced = f"```json\n{_pool_json(50)}\n```"
    assert await refresh_meme_pool(MockImageModel(text_response=fenced), db) is True
    assert len(await active_pool(db)) == 50


async def test_refresh_rejects_garbage_and_keeps_old_pool(db: Database) -> None:
    model = MockImageModel(text_response="ну такое, держи список: утро, кофе, мемы")
    assert await refresh_meme_pool(model, db) is False
    assert len(await active_pool(db)) == 100  # bundled baseline untouched
    assert await db.get_config(POOL_REFRESHED_KEY) == ""  # no fake freshness stamp


async def test_refresh_drops_profane_items_and_rejects_thin_result(db: Database) -> None:
    # 42 items, 3 profane → 39 clean < MIN_ITEMS(40) → the whole update is refused.
    model = MockImageModel(text_response=_pool_json(42, profane_first=3))
    assert await refresh_meme_pool(model, db) is False
    assert len(await active_pool(db)) == 100


async def test_refresh_survives_model_without_text_support(db: Database) -> None:
    class _NoText(ImageModel):
        name = "notext"

        async def generate(self, prompt: str, refs: Sequence[bytes] = (), **_: object) -> bytes:
            return b""

        async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:
            return 1.0

        async def pick_emoji(self, image: bytes) -> str:
            return "🙂"

    assert await refresh_meme_pool(_NoText(), db) is False  # ModelError swallowed


async def test_pool_is_stale_logic(db: Database) -> None:
    now = datetime.now(UTC)
    assert await pool_is_stale(db, days=7) is True  # never refreshed
    await db.set_config(POOL_REFRESHED_KEY, "не дата")
    assert await pool_is_stale(db, days=7) is True  # garbage stamp → refresh
    await db.set_config(POOL_REFRESHED_KEY, now.isoformat())
    assert await pool_is_stale(db, days=7, now=now + timedelta(days=2)) is False
    assert await pool_is_stale(db, days=7, now=now + timedelta(days=8)) is True


def test_strip_code_fences_variants() -> None:
    assert strip_code_fences('{"a": 1}') == '{"a": 1}'
    assert strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fences('```\n{"a": 1}\n```') == '{"a": 1}'
    assert strip_code_fences("```") == ""


def test_build_refresh_prompt_demands_strict_json() -> None:
    prompt = build_refresh_prompt([])
    assert "ТОЛЬКО валидным JSON" in prompt
    assert "рунет" in prompt
    assert "60/40" in prompt


async def test_loop_disabled_when_days_zero(db: Database) -> None:
    model = MockImageModel(text_response=_pool_json(50))
    await meme_refresh_loop(model, db, days=0)  # returns immediately, no call
    assert model.text_calls == []


async def test_loop_refreshes_stale_pool_then_sleeps(db: Database) -> None:
    model = MockImageModel(text_response=_pool_json(50))
    task = asyncio.create_task(meme_refresh_loop(model, db, days=7, check_every_s=3600))
    try:
        # First iteration runs immediately (pool never refreshed → stale).
        for _ in range(100):
            if model.text_calls:
                break
            await asyncio.sleep(0.01)
        assert len(model.text_calls) == 1
        assert len(await active_pool(db)) == 50
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
