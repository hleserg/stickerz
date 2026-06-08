"""Auto-moderation strikes → temporary bans.

A strike is recorded per rule violation (nudity in a photo, profane caption).
Active strikes (last 30 days) map to a ban duration; the ban is stored with an
expiry the access middleware enforces. Strikes older than 30 days simply fall
out of the active count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sticker_service.db import Database

# (threshold, ban duration) — highest reached threshold wins.
_THRESHOLDS: tuple[tuple[int, timedelta], ...] = (
    (30, timedelta(days=30)),
    (15, timedelta(days=1)),
    (10, timedelta(hours=2)),
)


def ban_for_strikes(count: int) -> timedelta | None:
    """Ban duration for a given active-strike count, or None if below threshold."""
    for threshold, duration in _THRESHOLDS:
        if count >= threshold:
            return duration
    return None


async def register_strike(
    db: Database, user_id: int, reason: str = ""
) -> tuple[int, datetime | None]:
    """Record a strike (with reason); if a threshold is hit, set a ban. (count, until)."""
    count = await db.add_strike(user_id, reason)
    duration = ban_for_strikes(count)
    until: datetime | None = None
    if duration is not None:
        until = datetime.now(UTC) + duration
        await db.set_ban(user_id, until)
    return count, until
