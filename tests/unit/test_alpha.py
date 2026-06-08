"""Tests for alpha-test data: applications, generation quotas, budget."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from sticker_service.db import DEFAULT_GENERATIONS, Database
from sticker_service.services import analytics, budget


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


# --- applications -----------------------------------------------------------


async def test_application_lifecycle(db: Database) -> None:
    await db.add_application(1, "alice", "из телеграм-канала")
    app = await db.get_application(1)
    assert app is not None and app.status == "pending" and app.source == "из телеграм-канала"
    assert [a.user_id for a in await db.list_applications("pending")] == [1]

    await db.set_application_status(1, "approved")
    assert (await db.get_application(1)).status == "approved"  # type: ignore[union-attr]
    assert await db.list_applications("pending") == []
    assert [a.user_id for a in await db.list_applications("approved")] == [1]


async def test_reapply_resets_to_pending(db: Database) -> None:
    await db.add_application(2, None, "src")
    await db.set_application_status(2, "rejected")
    await db.add_application(2, None, "src2")  # re-apply
    assert (await db.get_application(2)).status == "pending"  # type: ignore[union-attr]


# --- quotas -----------------------------------------------------------------


async def test_generations_default_and_ops(db: Database) -> None:
    assert await db.generations_left(5) == DEFAULT_GENERATIONS
    assert await db.consume_generation(5) == DEFAULT_GENERATIONS - 1
    await db.set_generations(5, 0)
    assert await db.consume_generation(5) == 0  # never negative
    assert await db.add_generations(5, 2) == 2


# --- budget -----------------------------------------------------------------


async def _add_generations_events(db: Database, n: int) -> None:
    for _ in range(n):
        await db.add_event(1, analytics.GENERATION_DONE, {})


async def test_budget_remaining_and_enough(db: Database) -> None:
    await budget.set_budget(db, 10)
    assert await budget.remaining_budget(db) == 10
    # cost_per_gen default 0.7 → 10 generations = $7
    await _add_generations_events(db, 10)
    assert round(await budget.total_cost(db), 2) == 7.0
    assert round(await budget.remaining_budget(db), 2) == 3.0
    assert await budget.enough_for(db, 2) is True  # 2*0.7=1.4 ≤ 3.0
    assert await budget.enough_for(db, 5) is False  # 5*0.7=3.5 > 3.0


async def test_budget_forecast_and_summary(db: Database) -> None:
    await budget.set_budget(db, 7)
    # 7 / 0.7 = 10 generations forecast at the start.
    assert await budget.forecast_generations(db) == 10
    await _add_generations_events(db, 8)  # spent 5.6, remaining 1.4 → 2 more
    assert await budget.forecast_generations(db) == 2
    line = await budget.summary_line(db)
    assert "$1.40" in line and "~2 генераций" in line


async def test_budget_alerts_fire_once_and_rearm(db: Database) -> None:
    await budget.set_budget(db, 20)
    assert await budget.pending_alerts(db) == []  # remaining 20 > 10
    await _add_generations_events(db, 15)  # cost 10.5 → remaining 9.5 (≤10)
    first = await budget.pending_alerts(db)
    assert len(first) == 1  # the $10 alert
    assert await budget.pending_alerts(db) == []  # not repeated
    await _add_generations_events(db, 8)  # cost ~16.1 → remaining ~3.9 (≤5)
    assert len(await budget.pending_alerts(db)) == 1  # the $5 alert
    # Raising the budget re-arms the alerts.
    await budget.set_budget(db, 100)
    assert await budget.pending_alerts(db) == []
