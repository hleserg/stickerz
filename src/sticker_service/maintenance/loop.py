"""Periodic housekeeping so disk/DB growth stays bounded between restarts.

The boot-time sweep alone is not enough on a long-lived container: at full
alpha burn the volume gains 1-2 GB/day (see docs/operations/CAPACITY.md), so
drafts, rejected-sheet dumps, analytics events and abandoned FSM rows must be
pruned while the bot runs. The loop fires once at startup and then daily, and warns admins before
the disk actually fills.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sticker_service.config import Settings, get_settings
from sticker_service.db import Database
from sticker_service.fsm_storage import SqliteStorage
from sticker_service.services import analytics
from sticker_service.services.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Sends a housekeeping alert to the admins (wired to bot.send_message in bot.py).
Notify = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class MaintenanceReport:
    """What one housekeeping pass actually did (logged + asserted in tests)."""

    drafts_removed: int
    rejected_removed: int
    events_pruned: int
    fsm_rows_swept: int
    disk_used_pct: int
    disk_alerted: bool


def disk_used_pct(settings: Settings) -> int:
    """Percent of the data_dir filesystem in use (0 when it cannot be measured)."""
    try:
        usage = shutil.disk_usage(settings.data_dir)
    except OSError:  # data_dir not created yet (first boot, tests)
        return 0
    if usage.total == 0:  # pragma: no cover - degenerate filesystem
        return 0
    return round(usage.used * 100 / usage.total)


async def run_once(
    *,
    orchestrator: Orchestrator,
    db: Database,
    storage: SqliteStorage,
    notify: Notify,
    settings: Settings | None = None,
) -> MaintenanceReport:
    """One pass: GC drafts and rejected sheets, prune events, sweep FSM, check disk."""
    settings = settings or get_settings()
    drafts = await orchestrator.gc_stale_drafts(older_than_days=settings.draft_retention_days)
    rejected = await orchestrator.gc_rejected_sheets(
        older_than_days=settings.rejected_retention_days
    )
    # generation_done is kept forever — the alpha budget counts it all-time.
    events = await db.prune_events(
        older_than_days=settings.events_retention_days,
        keep_events=(analytics.GENERATION_DONE,),
    )
    fsm_rows = await storage.sweep_stale(older_than_days=settings.fsm_retention_days)

    used_pct = disk_used_pct(settings)
    threshold = settings.disk_alert_threshold_pct
    alerted = bool(threshold) and used_pct >= threshold
    if alerted:
        await notify(
            f"⚠️ Диск заполнен на {used_pct}% (порог {threshold}%). "
            "Старые паки/логи пора чистить или расширять том — детали: "
            "docs/operations/CAPACITY.md."
        )
    if drafts or rejected or events or fsm_rows or alerted:
        logger.info(
            "maintenance: drafts=%d rejected=%d events=%d fsm=%d disk=%d%%",
            drafts,
            rejected,
            events,
            fsm_rows,
            used_pct,
        )
    return MaintenanceReport(drafts, rejected, events, fsm_rows, used_pct, alerted)


async def maintenance_loop(
    *,
    orchestrator: Orchestrator,
    db: Database,
    storage: SqliteStorage,
    notify: Notify,
) -> None:
    """Run housekeeping at boot and then every maintenance_interval_hours.

    A failed pass is logged and retried on the next tick — one bad iteration
    (e.g. a locked file) must not stop retention for the container's lifetime.
    A non-positive interval preserves the old run-once-at-boot behavior.
    """
    while True:
        try:
            await run_once(orchestrator=orchestrator, db=db, storage=storage, notify=notify)
        except Exception:
            logger.exception("maintenance pass failed; retrying on the next tick")
        interval_h = get_settings().maintenance_interval_hours
        if interval_h <= 0:
            return
        await asyncio.sleep(interval_h * 3600)
