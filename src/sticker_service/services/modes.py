"""Bot operating modes, switchable by the first admin.

debug   — only admins can use the bot; everyone else gets a friendly "in
          development" message; no limits. (implemented)
alpha   — open to all in the middleware; applications + free-generation quotas +
          USD budget gate pack creation to approved participants. (implemented)
beta    — not implemented yet (switching is refused).
prod    — not implemented yet (switching is refused).

One variable, four values; only ``debug`` and ``alpha`` are implemented. The
active mode is stored in the DB ``config`` table.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sticker_service.db import Database

DEBUG = "debug"
ALPHA = "alpha"
BETA = "beta"
PROD = "prod"

MODES: tuple[str, ...] = (DEBUG, ALPHA, BETA, PROD)
IMPLEMENTED: frozenset[str] = frozenset({DEBUG, ALPHA})
DISPLAY: dict[str, str] = {
    DEBUG: "Отладка",
    ALPHA: "Альфа-тест",
    BETA: "Бета-тест",
    PROD: "Боевой режим",
}
DEFAULT = DEBUG

_KEY = "mode"
_ALPHA_STARTED_KEY = "alpha_started_at"


def is_implemented(mode: str) -> bool:
    return mode in IMPLEMENTED


async def get_mode(db: Database) -> str:
    return await db.get_config(_KEY, DEFAULT)


async def set_mode(db: Database, mode: str) -> None:
    await db.set_config(_KEY, mode)
    # Stamp when the alpha first opened: stats and the budget count tester
    # activity from this moment, so pre-alpha dev/test runs never pollute them.
    # Set once — re-entering alpha after a maintenance toggle keeps the window.
    if mode == ALPHA and not await db.get_config(_ALPHA_STARTED_KEY, ""):
        await db.set_config(_ALPHA_STARTED_KEY, datetime.now(UTC).isoformat())


async def alpha_started_at(db: Database) -> str | None:
    """ISO time the alpha first opened, or None while it never has."""
    return await db.get_config(_ALPHA_STARTED_KEY, "") or None
