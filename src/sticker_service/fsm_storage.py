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
import json
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
            "CREATE TABLE IF NOT EXISTS fsm (key TEXT PRIMARY KEY, state TEXT, data TEXT)"
        )
        await conn.commit()
        return cls(conn)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        """Persist the state string (``None`` clears it) without touching data."""
        value = state.state if isinstance(state, State) else state
        await self._conn.execute(
            "INSERT INTO fsm(key, state) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state",
            (_key(key), value),
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
            "INSERT INTO fsm(key, data) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (_key(key), _encode(data)),
        )
        await self._conn.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        """Return the stored data dict for ``key`` (empty dict if none)."""
        async with self._conn.execute("SELECT data FROM fsm WHERE key=?", (_key(key),)) as cur:
            row = await cur.fetchone()
        return _decode(row[0] if row else None)

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        await self._conn.close()
