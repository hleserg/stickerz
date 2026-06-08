"""Alpha-test budget accounting (in USD) and low-budget alerts.

Total generations come from the ``generation_done`` analytics events; the total
cost is generations × a per-generation cost estimate (configurable). Alerts at
≤$10 and ≤$5 remaining fire once each (re-armed when the budget is raised).
"""

from __future__ import annotations

from sticker_service.db import Database
from sticker_service.services import analytics

DEFAULT_COST_PER_GEN = 0.7  # USD per generated pack (rough, §12)

_BUDGET_KEY = "alpha_budget"
_COST_KEY = "cost_per_gen"
_ALERT10 = "alert_budget_10"
_ALERT5 = "alert_budget_5"


async def get_budget(db: Database) -> float:
    return float(await db.get_config(_BUDGET_KEY, "0"))


async def set_budget(db: Database, dollars: int) -> None:
    await db.set_config(_BUDGET_KEY, str(int(dollars)))
    # Re-arm alerts for the new budget.
    await db.set_config(_ALERT10, "")
    await db.set_config(_ALERT5, "")


async def cost_per_gen(db: Database) -> float:
    return float(await db.get_config(_COST_KEY, str(DEFAULT_COST_PER_GEN)))


async def total_generations(db: Database) -> int:
    return await db.count_events(analytics.GENERATION_DONE)


async def total_cost(db: Database) -> float:
    return await total_generations(db) * await cost_per_gen(db)


async def remaining_budget(db: Database) -> float:
    return await get_budget(db) - await total_cost(db)


async def enough_for(db: Database, generations: int) -> bool:
    """True if the remaining budget covers ``generations`` more packs."""
    return await remaining_budget(db) >= generations * await cost_per_gen(db)


async def pending_alerts(db: Database) -> list[str]:
    """Return low-budget alert messages to broadcast (each threshold once)."""
    remaining = await remaining_budget(db)
    messages: list[str] = []
    if remaining <= 10 and not await db.get_config(_ALERT10):
        await db.set_config(_ALERT10, "1")
        messages.append(f"⚠️ Бюджет альфа-теста заканчивается: осталось ${remaining:.2f} (≤ $10).")
    if remaining <= 5 and not await db.get_config(_ALERT5):
        await db.set_config(_ALERT5, "1")
        messages.append(f"🔴 Бюджет почти исчерпан: осталось ${remaining:.2f} (≤ $5).")
    return messages
