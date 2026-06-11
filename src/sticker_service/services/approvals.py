"""Application approval — the one action shared by the admin button and alpha auto-approve.

Approving a tester is three writes (application status, whitelist, starting
credits) plus one welcome message. The manual admin button and the automatic
first-N path must stay byte-identical in effect, so both call here.
"""

from __future__ import annotations

import logging

from sticker_service.config import get_settings
from sticker_service.db import DEFAULT_CREDITS, Database
from sticker_service.services import modes, pricing

logger = logging.getLogger(__name__)

BUG_BONUS_PACKS = 2  # extra packs for a confirmed bug report


def welcome_text() -> str:
    """The greeting an approved tester receives (same for manual and auto)."""
    packs = pricing.format_packs(DEFAULT_CREDITS)
    return (
        f"🎉 Рады приветствовать вас в тестировании! Вам доступно "
        f"{packs} бесплатных паков (новый пак — 1, добавить стикеры — 0.5); "
        f"остаток всегда виден по команде /balance. "
        f"За каждый подтверждённый баг из /report начислим +{BUG_BONUS_PACKS} пака. "
        f"Что умеет бот — /help. Поехали: /new"
    )


async def approve_user(db: Database, user_id: int) -> None:
    """Mark the application approved, whitelist the user, grant starting credits.

    The application's @username is carried onto the whitelist row so the admin
    user list can show handles instead of bare ids.
    """
    app = await db.get_application(user_id)
    username = app.username if app is not None else None
    await db.set_application_status(user_id, "approved")
    await db.allow(user_id, username)
    await db.set_credits(user_id, DEFAULT_CREDITS)


async def maybe_auto_approve(db: Database, user_id: int) -> bool:
    """Auto-approve the application while the alpha has open auto seats.

    The first ``APP_ALPHA_AUTO_APPROVE_LIMIT`` approved testers (manual
    approvals count toward the seats) get in without waiting for the admin;
    everyone after that follows the normal manual review. Active only in
    alpha mode; 0 disables the feature. The seat count is read-then-written
    on one event loop, so a burst of simultaneous applications can overshoot
    by at most the burst size — acceptable for an alpha gate, not a billing
    invariant.
    """
    limit = get_settings().alpha_auto_approve_limit
    if limit <= 0 or await modes.get_mode(db) != modes.ALPHA:
        return False
    approved = len(await db.list_applications("approved"))
    if approved >= limit:
        return False
    await approve_user(db, user_id)
    logger.info("auto-approved tester %s (%d/%d seats)", user_id, approved + 1, limit)
    return True
