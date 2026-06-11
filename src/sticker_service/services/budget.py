"""Alpha-test budget accounting (in USD) and low-budget alerts.

Total generations come from the ``generation_done`` analytics events; the total
cost weights them by mode — a full pack (fresh) costs ``cost_per_gen``, while a
half-price action (reuse/extend: no canonical, just one sheet) costs half of
it, mirroring the real Gemini spend (§12, HLE-1040). Alerts at ≤$10 and ≤$5
remaining fire once each (re-armed when the budget is raised).
"""

from __future__ import annotations

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.services import analytics, modes

# USD per FULL generated pack. Measured June 2026 (services/cost.py prices):
# canonical 2–3 × pro@1K ($0.27–0.40) + sheet pro@4K ($0.24) + vision/refs
# (~$0.015) ≈ $0.52–0.66 → 0.65 is the conservative full-pack estimate.
DEFAULT_COST_PER_GEN = 0.65
# Pack-building modes billed to the user at half price — and costing roughly
# half for us too (one 4K sheet, no canonical pipeline).
HALF_COST_MODES = ("reuse", "extend")

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
    """Tester generations counted toward the budget (admins' own runs excluded)."""
    return await db.count_events(
        analytics.GENERATION_DONE,
        exclude_users=get_settings().admin_id_list,
        since=await modes.alpha_started_at(db),
    )


async def total_cost(db: Database) -> float:
    """Spend estimate from TESTER generations during the alpha only.

    Admins (the owner) bypass credit charging and generate for free while
    developing/testing — counting those would make the alpha budget look spent
    before a single tester arrives. Everyone's PRE-alpha runs were dev testing
    too, so the window opens at ``modes.alpha_started_at``. The $ budget tracks
    tester consumption within the alpha.
    """
    per = await cost_per_gen(db)
    admins = get_settings().admin_id_list
    since = await modes.alpha_started_at(db)
    total = await db.count_events(analytics.GENERATION_DONE, exclude_users=admins, since=since)
    half = await db.count_events_with_modes(
        analytics.GENERATION_DONE, HALF_COST_MODES, exclude_users=admins, since=since
    )
    return (total - half) * per + half * per / 2


async def remaining_budget(db: Database) -> float:
    return await get_budget(db) - await total_cost(db)


async def enough_for(db: Database, generations: int) -> bool:
    """True if the remaining budget covers ``generations`` more packs."""
    return await remaining_budget(db) >= generations * await cost_per_gen(db)


async def forecast_generations(db: Database) -> int:
    """How many more packs the remaining budget can fund (floored at 0)."""
    per = await cost_per_gen(db)
    if per <= 0:
        return 0
    return max(0, int(await remaining_budget(db) // per))


async def summary_line(db: Database) -> str:
    """One-line budget status for the admin /stats view."""
    spent = await total_cost(db)
    total = await get_budget(db)
    remaining = await remaining_budget(db)
    per = await cost_per_gen(db)
    forecast = await forecast_generations(db)
    return (
        f"Бюджет: ${remaining:.2f} из ${total:.0f} (потрачено ${spent:.2f}, "
        f"${per:.2f}/ген) — хватит ещё на ~{forecast} генераций."
    )


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
