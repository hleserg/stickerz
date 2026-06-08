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
