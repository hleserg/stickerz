"""Alpha product metrics (DAU/WAU, conversion, retention) for the admin /stats.

All counts exclude admins AND anything before the alpha opened
(``modes.alpha_started_at``): in alpha only testers act, so the owner's and
friends' pre-alpha dev/test activity must not pollute the numbers (mirrors
budget/funnel).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.services import analytics, modes


async def alpha_metrics_text(db: Database) -> str:
    """A compact tester-metrics block for the /stats caption."""
    admins = get_settings().admin_id_list
    started = await modes.alpha_started_at(db)
    now = datetime.now(UTC)
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    # ISO-8601 strings with the same offset compare lexicographically — the
    # rolling DAU/WAU windows simply must not reach back before the alpha.
    since_day = max(day_ago, started) if started else day_ago
    since_week = max(week_ago, started) if started else week_ago

    active = await db.count_distinct_users(since=started, exclude_users=admins)
    dau = await db.count_distinct_users(since=since_day, exclude_users=admins)
    wau = await db.count_distinct_users(since=since_week, exclude_users=admins)
    returning = await db.count_returning_users(since=started, exclude_users=admins)
    generators = await db.count_users_with_event(
        analytics.GENERATION_DONE, since=started, exclude_users=admins
    )
    gens = await db.count_events(analytics.GENERATION_DONE, since=started, exclude_users=admins)
    admin_set = set(admins)
    approved = sum(1 for e in await db.list_whitelist() if e.user_id not in admin_set)

    conv = f"{round(generators / approved * 100)}%" if approved else "—"
    avg = f"{gens / generators:.1f}" if generators else "—"
    return (
        "📈 Метрики (тестеры, без админов)\n"
        f"👥 Активные: {active} · DAU: {dau} · WAU: {wau}\n"
        f"🎯 Дошли до пака: {generators} из {approved} одобр. ({conv})\n"
        f"🔁 Вернулись (2+ дня): {returning}\n"
        f"📦 В среднем паков на тестера: {avg}"
    )
