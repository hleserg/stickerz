"""Bot operating modes, switchable by the first admin.

debug   — only admins can use the bot; everyone else gets a friendly "in
          development" message; no limits.
alpha   — not implemented yet.
beta    — not implemented yet (current closed test behaves like this).
prod    — normal operation (whitelist + moderation + limits).

The active mode is stored in the DB ``config`` table.
"""

from __future__ import annotations

from sticker_service.db import Database

DEBUG = "debug"
ALPHA = "alpha"
BETA = "beta"
PROD = "prod"

MODES: tuple[str, ...] = (DEBUG, ALPHA, BETA, PROD)
IMPLEMENTED: frozenset[str] = frozenset({DEBUG, PROD})
DISPLAY: dict[str, str] = {
    DEBUG: "Отладка",
    ALPHA: "Альфа-тест",
    BETA: "Бета-тест",
    PROD: "Боевой режим",
}
DEFAULT = DEBUG

_KEY = "mode"


def is_implemented(mode: str) -> bool:
    return mode in IMPLEMENTED


async def get_mode(db: Database) -> str:
    return await db.get_config(_KEY, DEFAULT)


async def set_mode(db: Database, mode: str) -> None:
    await db.set_config(_KEY, mode)
