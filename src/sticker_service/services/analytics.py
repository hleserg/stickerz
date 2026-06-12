"""Lightweight analytics: append discrete events to the DB for later stats.

Events are JSON-detailed rows; querying/aggregation is out of scope here (read
the ``events`` table directly). Failures never break the user flow.
"""

from __future__ import annotations

import logging

from sticker_service.db import Database

logger = logging.getLogger(__name__)

# Event names.
START = "start"
STYLE_CHOSEN = "style_chosen"
CAPTIONS_SELECTED = "captions_selected"
GENERATION_DONE = "generation_done"
GENERATION_ERROR = "generation_error"
DOWNLOADED = "downloaded"
PUBLISHED = "published"
EXTENDED = "extended"
SLICING_FALLBACK = "slicing_fallback"
CAPTION_GATE = "caption_gate"
SCENE_OBSERVER = "scene_observer"
# Logged at the moment credits are actually spent, so a refund can verify a
# charge really happened instead of gifting credits on top.
CREDITS_CHARGED = "credits_charged"
# Logged when the owner refunds a charge; a charge older than the latest
# refund is considered settled and can never be refunded again.
CREDITS_REFUNDED = "credits_refunded"


async def log(db: Database, user_id: int, event: str, **detail: object) -> None:
    """Record an event; swallow errors so analytics never breaks the flow."""
    try:
        await db.add_event(user_id, event, detail)
    except Exception as exc:  # analytics must never break the user flow
        logger.warning("analytics log failed (%s): %s", event, str(exc)[:100])


async def track_start(db: Database, user_id: int) -> bool:
    """Record a start event with the new/returning flag; return is_returning."""
    returning = False
    try:
        returning = await db.has_events(user_id)
    except Exception:  # pragma: no cover - defensive
        returning = False
    await log(db, user_id, START, returning=returning)
    return returning
