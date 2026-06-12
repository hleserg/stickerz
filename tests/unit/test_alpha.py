"""Tests for alpha-test data: applications, generation quotas, budget."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from sticker_service.db import DEFAULT_CREDITS, Database
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


async def test_credits_default_and_ops(db: Database) -> None:
    assert await db.credits_left(5) == DEFAULT_CREDITS  # 6 half-packs = 3 packs
    assert await db.consume_credits(5, 2) == (DEFAULT_CREDITS - 2, True)  # spend 1 pack
    assert await db.consume_credits(5, 1) == (DEFAULT_CREDITS - 3, True)  # spend 0.5 pack
    await db.set_credits(5, 0)
    assert await db.consume_credits(5, 2) == (0, False)  # never negative, nothing spent
    assert await db.add_credits(5, 2) == 2


async def test_hot_path_indexes_exist(db: Database) -> None:
    # The per-user / per-pack lookups and the budget count must be indexed.
    async with db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'") as cur:
        names = {row["name"] for row in await cur.fetchall()}
    for expected in ("idx_stickers_pack_id", "idx_packs_owner_id", "idx_events_event"):
        assert expected in names


async def test_consume_credits_atomic_under_concurrency(db: Database) -> None:
    # Two concurrent spends must BOTH apply — the old read-modify-write could
    # interleave (read 6, read 6, write 5, write 5) and lose a decrement.
    import asyncio

    await db.set_credits(9, 6)
    await asyncio.gather(db.consume_credits(9, 1), db.consume_credits(9, 1))
    assert await db.credits_left(9) == 4  # both decrements landed, not 5


async def test_consume_credits_all_or_nothing_when_insufficient(db: Database) -> None:
    # Spending more than the balance leaves it untouched (no partial debit),
    # and the caller is told the spend never happened (no phantom charge).
    await db.set_credits(11, 1)
    assert await db.consume_credits(11, 2) == (1, False)  # insufficient → nothing spent


# --- budget -----------------------------------------------------------------


async def _add_generations_events(db: Database, n: int, mode: str = "fresh") -> None:
    for _ in range(n):
        await db.add_event(1, analytics.GENERATION_DONE, {"mode": mode})


async def test_budget_remaining_and_enough(db: Database) -> None:
    await budget.set_budget(db, 10)
    assert await budget.remaining_budget(db) == 10
    # cost_per_gen default 0.65 → 10 full generations = $6.50
    await _add_generations_events(db, 10)
    assert round(await budget.total_cost(db), 2) == 6.5
    assert round(await budget.remaining_budget(db), 2) == 3.5
    assert await budget.enough_for(db, 2) is True  # 2*0.65=1.3 ≤ 3.5
    assert await budget.enough_for(db, 6) is False  # 6*0.65=3.9 > 3.5


async def test_budget_excludes_admin_generations(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Admins (the owner) generate for free while testing — their runs must not
    # count toward the alpha tester budget.
    from sticker_service.config import get_settings

    monkeypatch.setenv("APP_ADMIN_IDS", "777")
    get_settings.cache_clear()
    try:
        for _ in range(5):  # admin's own dev/test generations
            await db.add_event(777, analytics.GENERATION_DONE, {"mode": "fresh"})
        await db.add_event(42, analytics.GENERATION_DONE, {"mode": "fresh"})  # a real tester
        assert round(await budget.total_cost(db), 2) == 0.65  # only the tester's one pack
        assert await budget.total_generations(db) == 1
    finally:
        get_settings.cache_clear()


async def test_budget_counts_half_price_modes_at_half_cost(db: Database) -> None:
    # reuse/extend skip the canonical pipeline (one sheet only), so the spend
    # estimate weights them at cost_per_gen/2 — not the full-pack price.
    await _add_generations_events(db, 2, mode="fresh")
    await _add_generations_events(db, 1, mode="reuse")
    await _add_generations_events(db, 1, mode="extend")
    assert round(await budget.total_cost(db), 3) == round(2 * 0.65 + 2 * 0.325, 3)


async def test_budget_forecast_and_summary(db: Database) -> None:
    # The measured full-pack estimate ships as the default…
    assert budget.DEFAULT_COST_PER_GEN == 0.65
    # …while the math test pins cost_per_gen to a binary-exact 0.5 so the
    # floor-division forecast is deterministic (no float-epsilon off-by-one).
    await db.set_config("cost_per_gen", "0.5")
    await budget.set_budget(db, 10)
    assert await budget.forecast_generations(db) == 20
    await _add_generations_events(db, 16)  # spent 8.0, remaining 2.0 → 4 more
    assert await budget.forecast_generations(db) == 4
    line = await budget.summary_line(db)
    assert "$2.00" in line and "~4 генераций" in line


async def test_budget_alerts_fire_once_and_rearm(db: Database) -> None:
    await budget.set_budget(db, 20)
    assert await budget.pending_alerts(db) == []  # remaining 20 > 10
    await _add_generations_events(db, 16)  # cost 10.4 → remaining 9.6 (≤10)
    first = await budget.pending_alerts(db)
    assert len(first) == 1  # the $10 alert
    assert await budget.pending_alerts(db) == []  # not repeated
    await _add_generations_events(db, 8)  # cost 15.6 → remaining 4.4 (≤5)
    assert len(await budget.pending_alerts(db)) == 1  # the $5 alert
    # Raising the budget re-arms the alerts.
    await budget.set_budget(db, 100)
    assert await budget.pending_alerts(db) == []


# --- events: mode counting + retention ---------------------------------------


async def test_count_events_with_modes(db: Database) -> None:
    await _add_generations_events(db, 3, mode="fresh")
    await _add_generations_events(db, 2, mode="extend")
    await db.add_event(1, analytics.GENERATION_DONE, {})  # legacy row, no mode
    n = await db.count_events_with_modes(analytics.GENERATION_DONE, ("reuse", "extend"))
    assert n == 2
    assert await db.count_events_with_modes(analytics.GENERATION_DONE, ()) == 0


async def test_prune_events_keeps_budget_critical_rows(db: Database) -> None:
    # Old chatter is pruned, but generation_done is kept at ANY age — the alpha
    # budget counts it all-time, and pruning it would inflate the remaining budget.
    await db.add_event(1, analytics.START, {})
    await db.add_event(1, analytics.GENERATION_DONE, {"mode": "fresh"})
    await db._conn.execute("UPDATE events SET created_at = '2000-01-01T00:00:00+00:00'")
    await db._conn.commit()
    removed = await db.prune_events(older_than_days=30, keep_events=(analytics.GENERATION_DONE,))
    assert removed == 1
    assert await db.count_events(analytics.START) == 0
    assert await db.count_events(analytics.GENERATION_DONE) == 1
    assert await db.prune_events(older_than_days=0, keep_events=()) == 0  # disabled
