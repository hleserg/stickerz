"""Tests for analytics event logging."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from sticker_service.db import Database
from sticker_service.services import analytics


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


async def test_log_records_event(db: Database) -> None:
    await analytics.log(db, 1, analytics.STYLE_CHOSEN, style_id="watercolor")
    assert await db.count_events(analytics.STYLE_CHOSEN) == 1
    assert await db.has_events(1) is True


async def test_track_start_new_then_returning(db: Database) -> None:
    assert await analytics.track_start(db, 7) is False  # first time → new
    assert await analytics.track_start(db, 7) is True  # second time → returning
    assert await db.count_events(analytics.START) == 2


async def test_log_never_raises(db: Database) -> None:
    await db.close()  # force the underlying connection to fail
    # Should swallow the error rather than propagate.
    await analytics.log(db, 1, analytics.DOWNLOADED, count=3)
