"""Tests for the meme-idea pool: bundled data, validation, default-mix sampling."""

from __future__ import annotations

import json
import random

import pytest

from sticker_service.db import Database
from sticker_service.services.stickers import MAX_CAPTIONS, STANDARD_BLOCK
from sticker_service.services.stickers.meme_pool import (
    MIN_ITEMS,
    POOL_CONFIG_KEY,
    MemeIdea,
    active_pool,
    bundled_pool,
    parse_pool,
    sample_default_mix,
)


def _pool_json(n: int) -> str:
    items = [{"category": "к", "description": f"идея {i}", "caption": None} for i in range(n)]
    return json.dumps({"items": items}, ensure_ascii=False)


def test_bundled_pool_loads_100_curated_ideas() -> None:
    pool = bundled_pool()
    assert len(pool) == 100
    assert all(idea.description for idea in pool)
    assert any(idea.caption is None for idea in pool)  # «Без подписи» entries survive
    assert any(idea.caption == "Я проснулась. Технически." for idea in pool)
    assert any(idea.caption == "База." for idea in pool)


def test_as_sheet_item_quotes_caption_or_forbids_text() -> None:
    with_cap = MemeIdea(category="x", description="Пьёт кофе.", caption="Первый глоток.")
    without = MemeIdea(category="x", description="Спит лицом в подушку.", caption=None)
    assert with_cap.as_sheet_item() == "Пьёт кофе. Подпись: «Первый глоток.»"
    assert without.as_sheet_item() == "Спит лицом в подушку. Без подписи."


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        json.dumps({"items": "nope"}),
        _pool_json(3),  # too few items
        json.dumps({"items": [{"category": "к", "description": "", "caption": None}] * 50}),
        json.dumps({"items": [{"description": "ок", "caption": "x" * 200}] * 50}),
        json.dumps({"items": [["not", "an", "object"]] * 50}),
    ],
)
def test_parse_pool_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValueError, match="meme pool"):
        parse_pool(raw)


def test_parse_pool_drops_duplicate_descriptions() -> None:
    items = json.loads(_pool_json(MIN_ITEMS))["items"]
    items += [{"category": "к", "description": "идея 0", "caption": None}] * 5
    pool = parse_pool(json.dumps({"items": items}))
    assert len(pool) == MIN_ITEMS  # dupes folded, not fatal


def test_sample_default_mix_is_13_unique_items() -> None:
    pool = bundled_pool()
    sheet_items = {idea.as_sheet_item() for idea in pool}
    for seed in range(20):
        std, memes = sample_default_mix(pool, rng=random.Random(seed))
        assert len(std) + len(memes) == 13  # ALWAYS 13 pre-filled items
        assert 6 <= len(std) <= 8
        assert 5 <= len(memes) <= 7
        assert len(std) + len(memes) < MAX_CAPTIONS  # room left for user ideas
        assert std == sorted(set(std))  # unique, ordered like the checklist
        assert len(set(memes)) == len(memes)  # no repeated meme ideas
        assert all(0 <= i < len(STANDARD_BLOCK) for i in std)
        assert all(m in sheet_items for m in memes)


def test_sample_default_mix_deterministic_per_seed() -> None:
    pool = bundled_pool()
    first = sample_default_mix(pool, rng=random.Random(7))
    assert sample_default_mix(pool, rng=random.Random(7)) == first
    assert sample_default_mix(pool, rng=random.Random(8)) != first


def test_sample_default_mix_with_tiny_pool() -> None:
    pool = [MemeIdea(category="к", description=f"идея {i}", caption=None) for i in range(3)]
    std, memes = sample_default_mix(pool, rng=random.Random(1))
    assert len(memes) == 3  # pool smaller than the 5-7 ask → take what's there
    assert 5 <= len(std) <= 8


async def test_active_pool_prefers_valid_db_copy_and_falls_back() -> None:
    db = await Database.connect(":memory:")
    try:
        assert len(await active_pool(db)) == 100  # nothing stored → bundled
        await db.set_config(POOL_CONFIG_KEY, "{broken json")
        assert len(await active_pool(db)) == 100  # invalid stored copy → bundled
        await db.set_config(POOL_CONFIG_KEY, _pool_json(50))
        assert len(await active_pool(db)) == 50  # valid stored copy wins
    finally:
        await db.close()
