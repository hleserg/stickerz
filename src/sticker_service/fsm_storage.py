"""Persistent FSM storage on SQLite so a restart never loses an in-flight flow.

aiogram's default ``MemoryStorage`` keeps FSM state in RAM, so a worker restart —
e.g. an OOM kill on a small VDS — silently drops every user mid-flow and the bot
just appears to "hang" with no error. This storage persists state and data to a
SQLite file, so a flow resumes exactly where it left off after a restart.

FSM data here may hold raw ``bytes`` (the uploaded photo, generated sticker
images) which JSON can't encode, so a tiny base64 codec round-trips bytes
wherever they appear in the data dict.
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey


def _default(obj: Any) -> Any:
    """JSON fallback: encode raw bytes as a tagged base64 string."""
    if isinstance(obj, bytes):
        return {"__b64__": base64.b64encode(obj).decode("ascii")}
    raise TypeError(f"not JSON-serializable: {type(obj).__name__}")


def _object_hook(obj: dict[str, Any]) -> Any:
    """JSON reviver: restore tagged base64 strings back to bytes."""
    if len(obj) == 1 and "__b64__" in obj:
        return base64.b64decode(obj["__b64__"])
    return obj


def _encode(data: Mapping[str, Any]) -> str:
    return json.dumps(dict(data), default=_default, ensure_ascii=False)


def _decode(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    return json.loads(raw, object_hook=_object_hook)


def _key(key: StorageKey) -> str:
    """Flatten a :class:`StorageKey` into a stable primary-key string."""
    return (
        f"{key.bot_id}:{key.chat_id}:{key.user_id}:"
        f"{key.thread_id}:{key.business_connection_id}:{key.destiny}"
    )


class SqliteStorage(BaseStorage):
    """An aiogram FSM storage backed by a single SQLite file (survives restarts)."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def create(cls, path: str | Path) -> SqliteStorage:
        """Open (creating if needed) the SQLite file and ensure the schema."""
        conn = await aiosqlite.connect(path)
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS fsm "
            "(key TEXT PRIMARY KEY, state TEXT, data TEXT, updated_at REAL)"
        )
        # Migrate pre-updated_at files in place. Existing rows get "now", not 0:
        # an in-flight flow on the production volume must not look ancient (and
        # be swept) right after the deploy that introduced the column.
        with contextlib.suppress(aiosqlite.OperationalError):  # column already exists
            await conn.execute("ALTER TABLE fsm ADD COLUMN updated_at REAL")
        await conn.execute("UPDATE fsm SET updated_at=? WHERE updated_at IS NULL", (time.time(),))
        await conn.commit()
        return cls(conn)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        """Persist the state string (``None`` clears it) without touching data."""
        value = state.state if isinstance(state, State) else state
        await self._conn.execute(
            "INSERT INTO fsm(key, state, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (_key(key), value, time.time()),
        )
        await self._conn.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        """Return the stored state string for ``key`` (or ``None``)."""
        async with self._conn.execute("SELECT state FROM fsm WHERE key=?", (_key(key),)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        """Persist the full data dict (replace) without touching the state."""
        await self._conn.execute(
            "INSERT INTO fsm(key, data, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (_key(key), _encode(data), time.time()),
        )
        await self._conn.commit()

    async def sweep_stale(self, *, older_than_days: int) -> int:
        """Drop leftover rows so fsm.sqlite stays small on a long-lived volume.

        Two kinds of garbage accumulate by design: rows fully cleared by
        ``state.clear()`` (state NULL, empty data — pure leftovers, any age) and
        flows abandoned mid-wizard (any state, untouched for the retention
        window — resuming them weeks later is meaningless). Returns the number
        of rows removed; the file is VACUUMed only when something was removed.
        A non-positive window keeps abandoned flows and only drops cleared rows.
        """
        cutoff = time.time() - older_than_days * 86400 if older_than_days > 0 else None
        cur = await self._conn.execute(
            "DELETE FROM fsm WHERE (state IS NULL AND (data IS NULL OR data IN ('', '{}'))) "
            "OR (? IS NOT NULL AND updated_at < ?)",
            (cutoff, cutoff),
        )
        removed = cur.rowcount or 0
        await self._conn.commit()
        if removed:
            await self._conn.execute("VACUUM")
            await self._conn.commit()
        return removed

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        """Return the stored data dict for ``key`` (empty dict if none)."""
        async with self._conn.execute("SELECT data FROM fsm WHERE key=?", (_key(key),)) as cur:
            row = await cur.fetchone()
        return _decode(row[0] if row else None)

    async def keys_in_state(
        self, state: str, *, max_age_seconds: float | None = None
    ) -> list[tuple[int, int, int]]:
        """``(bot_id, chat_id, user_id)`` of every row currently in ``state``.

        Lets the boot path find flows orphaned mid-generation by a hard restart
        (the worker died, but the persistent state row still says "publish").
        ``max_age_seconds`` keeps only rows touched recently — a row stuck for
        days is an abandoned flow, not an interrupted one.
        """
        out: list[tuple[int, int, int]] = []
        if max_age_seconds is not None:
            cutoff = time.time() - max_age_seconds
            query = self._conn.execute(
                "SELECT key FROM fsm WHERE state=? AND updated_at >= ?", (state, cutoff)
            )
        else:
            query = self._conn.execute("SELECT key FROM fsm WHERE state=?", (state,))
        async with query as cur:
            rows = await cur.fetchall()
        for (raw,) in rows:
            parts = str(raw).split(":")
            with contextlib.suppress(ValueError, IndexError):
                out.append((int(parts[0]), int(parts[1]), int(parts[2])))
        return out

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        await self._conn.close()
