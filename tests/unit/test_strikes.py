"""Tests for the strikes → ban auto-moderation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest_asyncio

from sticker_service.db import Database
from sticker_service.services.strikes import ban_for_strikes, register_strike


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def test_ban_for_strikes_thresholds() -> None:
    assert ban_for_strikes(9) is None
    assert ban_for_strikes(10) == timedelta(hours=2)
    assert ban_for_strikes(14) == timedelta(hours=2)
    assert ban_for_strikes(15) == timedelta(days=1)
    assert ban_for_strikes(30) == timedelta(days=30)


async def test_strike_counts_and_no_ban_before_threshold(db: Database) -> None:
    count = 0
    for _ in range(9):
        count, until = await register_strike(db, 1)
        assert until is None
    assert count == 9
    assert await db.active_strikes(1) == 9
    assert await db.banned_until(1) is None


async def test_strike_sets_ban_at_threshold(db: Database) -> None:
    until = None
    for _ in range(10):
        _count, until = await register_strike(db, 7)
    assert until is not None
    assert await db.banned_until(7) is not None  # currently banned


async def test_banned_until_none_when_expired(db: Database) -> None:
    from datetime import UTC, datetime

    await db.set_ban(5, datetime.now(UTC) - timedelta(hours=1))  # already past
    assert await db.banned_until(5) is None


async def test_strike_reason_listed(db: Database) -> None:
    await register_strike(db, 3, "На фото обнажёнка")
    await register_strike(db, 3, "Так нельзя (мат…)")
    strikes = await db.list_strikes(3)
    reasons = [r for r, _ in strikes]
    assert "На фото обнажёнка" in reasons
    assert len(strikes) == 2


async def test_list_bans_and_unban(db: Database) -> None:
    from datetime import UTC, datetime

    await db.set_ban(8, datetime.now(UTC) + timedelta(hours=1))
    assert [uid for uid, _ in await db.list_bans()] == [8]
    await db.unban(8)
    assert await db.list_bans() == []


async def test_config_get_set(db: Database) -> None:
    assert await db.get_config("k", "def") == "def"
    await db.set_config("k", "v")
    assert await db.get_config("k") == "v"
