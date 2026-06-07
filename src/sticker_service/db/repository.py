"""Async SQLite data-access layer (aiosqlite, no heavy ORM).

A single :class:`Database` wraps one connection and exposes typed repository
methods. Schema is created on connect via idempotent DDL — migrations stay
simple for the MVP. Handlers call these methods; they never write raw SQL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from sticker_service.db.models import (
    Character,
    Order,
    Pack,
    Sticker,
    SubjectType,
    WhitelistEntry,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS whitelist (
    user_id  INTEGER PRIMARY KEY,
    username TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS characters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id       INTEGER NOT NULL,
    name           TEXT NOT NULL,
    style_id       TEXT NOT NULL,
    subject_type   TEXT NOT NULL,
    child_age      INTEGER,
    canonical_path TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id),
    owner_id     INTEGER NOT NULL,
    set_name     TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stickers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    pack_id    INTEGER NOT NULL REFERENCES packs(id),
    file_path  TEXT NOT NULL,
    emoji      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    owner_id   INTEGER PRIMARY KEY,
    state      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consents (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id  INTEGER NOT NULL,
    agreed_at TEXT NOT NULL
);
"""


def _now() -> datetime:
    return datetime.now(UTC)


class Database:
    """Owns an aiosqlite connection and the typed repository methods."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, path: str | Path) -> Database:
        """Open (creating parent dirs), enable FKs, and apply the schema."""
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        db = cls(conn)
        await conn.execute("PRAGMA foreign_keys = ON")
        await db._init_schema()
        return db

    async def _init_schema(self) -> None:
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        await self._conn.close()

    # --- whitelist (§11.1) ---------------------------------------------------

    async def allow(self, user_id: int, username: str | None = None) -> None:
        """Add or refresh a whitelisted user (user_id is the durable key)."""
        await self._conn.execute(
            "INSERT INTO whitelist (user_id, username, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username",
            (user_id, username, _now().isoformat()),
        )
        await self._conn.commit()

    async def deny(self, user_id: int) -> None:
        await self._conn.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    async def is_allowed(self, user_id: int) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def list_whitelist(self) -> list[WhitelistEntry]:
        async with self._conn.execute(
            "SELECT user_id, username, added_at FROM whitelist ORDER BY added_at"
        ) as cur:
            rows = await cur.fetchall()
        return [
            WhitelistEntry(user_id=r["user_id"], username=r["username"], added_at=r["added_at"])
            for r in rows
        ]

    # --- characters (§3.2) ---------------------------------------------------

    async def add_character(
        self,
        *,
        owner_id: int,
        name: str,
        style_id: str,
        subject_type: SubjectType,
        canonical_path: str,
        child_age: int | None = None,
    ) -> Character:
        """Persist a confirmed canonical character and return it with its id."""
        created = _now()
        cur = await self._conn.execute(
            "INSERT INTO characters "
            "(owner_id, name, style_id, subject_type, child_age, canonical_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                owner_id,
                name,
                style_id,
                subject_type,
                child_age,
                canonical_path,
                created.isoformat(),
            ),
        )
        await self._conn.commit()
        return Character(
            id=cur.lastrowid or 0,
            owner_id=owner_id,
            name=name,
            style_id=style_id,
            subject_type=subject_type,
            child_age=child_age,
            canonical_path=canonical_path,
            created_at=created,
        )

    async def get_character(self, character_id: int) -> Character | None:
        async with self._conn.execute(
            "SELECT * FROM characters WHERE id = ?", (character_id,)
        ) as cur:
            row = await cur.fetchone()
        return Character(**dict(row)) if row else None

    async def list_characters(self, owner_id: int) -> list[Character]:
        async with self._conn.execute(
            "SELECT * FROM characters WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [Character(**dict(r)) for r in rows]

    # --- packs ---------------------------------------------------------------

    async def add_pack(
        self, *, character_id: int, owner_id: int, set_name: str, title: str
    ) -> Pack:
        """Create a pack bound to a character (one character → many packs)."""
        created = _now()
        cur = await self._conn.execute(
            "INSERT INTO packs (character_id, owner_id, set_name, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (character_id, owner_id, set_name, title, created.isoformat()),
        )
        await self._conn.commit()
        return Pack(
            id=cur.lastrowid or 0,
            character_id=character_id,
            owner_id=owner_id,
            set_name=set_name,
            title=title,
            created_at=created,
        )

    async def get_pack(self, pack_id: int) -> Pack | None:
        async with self._conn.execute("SELECT * FROM packs WHERE id = ?", (pack_id,)) as cur:
            row = await cur.fetchone()
        return Pack(**dict(row)) if row else None

    async def get_pack_by_set_name(self, set_name: str) -> Pack | None:
        async with self._conn.execute("SELECT * FROM packs WHERE set_name = ?", (set_name,)) as cur:
            row = await cur.fetchone()
        return Pack(**dict(row)) if row else None

    async def list_packs(self, owner_id: int) -> list[Pack]:
        async with self._conn.execute(
            "SELECT * FROM packs WHERE owner_id = ? ORDER BY created_at DESC", (owner_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [Pack(**dict(r)) for r in rows]

    # --- stickers ------------------------------------------------------------

    async def add_sticker(
        self, *, pack_id: int, file_path: str, emoji: str, position: int
    ) -> Sticker:
        created = _now()
        cur = await self._conn.execute(
            "INSERT INTO stickers (pack_id, file_path, emoji, position, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (pack_id, file_path, emoji, position, created.isoformat()),
        )
        await self._conn.commit()
        return Sticker(
            id=cur.lastrowid or 0,
            pack_id=pack_id,
            file_path=file_path,
            emoji=emoji,
            position=position,
            created_at=created,
        )

    async def list_stickers(self, pack_id: int) -> list[Sticker]:
        async with self._conn.execute(
            "SELECT * FROM stickers WHERE pack_id = ? ORDER BY position", (pack_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [Sticker(**dict(r)) for r in rows]

    async def count_stickers(self, pack_id: int) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM stickers WHERE pack_id = ?", (pack_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    # --- order (in-progress build) -------------------------------------------

    async def save_order(self, owner_id: int, state: dict[str, object]) -> Order:
        """Upsert the user's in-progress build state."""
        updated = _now()
        await self._conn.execute(
            "INSERT INTO orders (owner_id, state, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(owner_id) DO UPDATE SET state = excluded.state, "
            "updated_at = excluded.updated_at",
            (owner_id, json.dumps(state), updated.isoformat()),
        )
        await self._conn.commit()
        return Order(owner_id=owner_id, state=state, updated_at=updated)

    async def get_order(self, owner_id: int) -> Order | None:
        async with self._conn.execute(
            "SELECT * FROM orders WHERE owner_id = ?", (owner_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return Order(
            owner_id=row["owner_id"], state=json.loads(row["state"]), updated_at=row["updated_at"]
        )

    async def clear_order(self, owner_id: int) -> None:
        await self._conn.execute("DELETE FROM orders WHERE owner_id = ?", (owner_id,))
        await self._conn.commit()

    # --- consent (§15.2) -----------------------------------------------------

    async def record_consent(self, owner_id: int) -> datetime:
        """Record the photo-rights consent fact + timestamp."""
        ts = _now()
        await self._conn.execute(
            "INSERT INTO consents (owner_id, agreed_at) VALUES (?, ?)",
            (owner_id, ts.isoformat()),
        )
        await self._conn.commit()
        return ts

    async def has_consent(self, owner_id: int) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM consents WHERE owner_id = ? LIMIT 1", (owner_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def open_database(paths: Sequence[str | Path] | None = None) -> Database:  # pragma: no cover
    """Convenience opener used by the bot runner (covered indirectly)."""
    from sticker_service.config import get_settings

    target = paths[0] if paths else get_settings().data_dir / "sticker_service.sqlite"
    return await Database.connect(target)
