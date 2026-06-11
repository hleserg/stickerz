"""Alpha product metrics (DAU/WAU, conversion, retention) for the admin /stats.

All counts exclude admins: in alpha only testers act, so the owner's own
dev/test activity must not pollute the numbers (mirrors budget/funnel).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.services import analytics


async def alpha_metrics_text(db: Database) -> str:
    """A compact tester-metrics block for the /stats caption."""
    admins = get_settings().admin_id_list
    now = datetime.now(UTC)
    day_ago = (now - timedelta(days=1)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    active = await db.count_distinct_users(exclude_users=admins)
    dau = await db.count_distinct_users(since=day_ago, exclude_users=admins)
    wau = await db.count_distinct_users(since=week_ago, exclude_users=admins)
    returning = await db.count_returning_users(exclude_users=admins)
    generators = await db.count_users_with_event(analytics.GENERATION_DONE, exclude_users=admins)
    gens = await db.count_events(analytics.GENERATION_DONE, exclude_users=admins)
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
