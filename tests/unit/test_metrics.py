"""Tests for the alpha product-metrics block on /stats."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.services import analytics, metrics


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ADMIN_IDS", "1")  # user 1 is an admin → excluded
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _event_at(db: Database, user_id: int, event: str, when: datetime) -> None:
    await db._conn.execute(
        "INSERT INTO events (user_id, event, detail, created_at) VALUES (?, ?, '{}', ?)",
        (user_id, event, when.isoformat()),
    )
    await db._conn.commit()


async def test_distinct_and_returning_users(db: Database) -> None:
    now = datetime.now(UTC)
    # user 2 active on two days → returning; user 3 one day; user 1 is admin.
    await _event_at(db, 2, analytics.START, now)
    await _event_at(db, 2, analytics.GENERATION_DONE, now - timedelta(days=3))
    await _event_at(db, 3, analytics.START, now)
    await _event_at(db, 1, analytics.START, now)  # admin
    assert await db.count_distinct_users(exclude_users=[1]) == 2  # users 2 and 3
    assert (
        await db.count_distinct_users(
            since=(now - timedelta(hours=1)).isoformat(), exclude_users=[1]
        )
        == 2
    )  # both active today
    assert await db.count_returning_users(exclude_users=[1]) == 1  # only user 2


async def test_users_with_event_excludes_admin(db: Database) -> None:
    now = datetime.now(UTC)
    await _event_at(db, 2, analytics.GENERATION_DONE, now)
    await _event_at(db, 2, analytics.GENERATION_DONE, now)  # same tester, 2 gens
    await _event_at(db, 1, analytics.GENERATION_DONE, now)  # admin gen, ignored
    assert await db.count_users_with_event(analytics.GENERATION_DONE, exclude_users=[1]) == 1
    assert await db.count_events(analytics.GENERATION_DONE, exclude_users=[1]) == 2


async def test_metrics_text_conversion_and_average(db: Database) -> None:
    now = datetime.now(UTC)
    await db.allow(2, "alice")  # approved tester
    await db.allow(3, "bob")  # approved tester, never generated
    await db.allow(1, "owner")  # admin in whitelist → excluded from "approved"
    await _event_at(db, 2, analytics.GENERATION_DONE, now)
    await _event_at(db, 2, analytics.GENERATION_DONE, now)  # 2 packs by one tester

    text = await metrics.alpha_metrics_text(db)
    assert "Дошли до пака: 1 из 2 одобр. (50%)" in text  # 1 of 2 testers generated
    assert "В среднем паков на тестера: 2.0" in text  # 2 gens / 1 generator


async def test_metrics_and_counts_window_from_alpha_start(db: Database) -> None:
    # Pre-alpha dev activity (the owner's friends helping test) must not leak
    # into tester metrics: counts open at modes.alpha_started_at.
    from sticker_service.services import modes

    now = datetime.now(UTC)
    await _event_at(db, 2, analytics.GENERATION_DONE, now - timedelta(days=2))  # pre-alpha
    await modes.set_mode(db, modes.ALPHA)  # stamps the window between the two
    started = await modes.alpha_started_at(db)
    assert started is not None
    await _event_at(db, 2, analytics.GENERATION_DONE, datetime.now(UTC))  # during alpha

    assert await db.count_events(analytics.GENERATION_DONE, exclude_users=[1]) == 2
    assert await db.count_events(analytics.GENERATION_DONE, exclude_users=[1], since=started) == 1
    assert (
        await db.count_users_with_event(analytics.GENERATION_DONE, exclude_users=[1], since=started)
        == 1
    )
    # Two active days overall, but only one inside the alpha → not "returning".
    assert await db.count_returning_users(exclude_users=[1]) == 1
    assert await db.count_returning_users(exclude_users=[1], since=started) == 0

    await db.allow(2, "alice")
    text = await metrics.alpha_metrics_text(db)
    assert "Дошли до пака: 1 из 1 одобр. (100%)" in text
    assert "В среднем паков на тестера: 1.0" in text  # pre-alpha gen not counted
