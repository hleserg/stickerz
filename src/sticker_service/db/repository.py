"""Async SQLite data-access layer (aiosqlite, no heavy ORM).

A single :class:`Database` wraps one connection and exposes typed repository
methods. Schema is created on connect via idempotent DDL — migrations stay
simple for the MVP. Handlers call these methods; they never write raw SQL.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from sticker_service.db.models import (
    Application,
    Character,
    Order,
    Pack,
    Sticker,
    SubjectType,
    WhitelistEntry,
)

# Generation credits are stored in HALF-PACKS (1 pack = 2 credits) so a
# half-pack action (adding stickers) is a whole integer — no float drift.
CREDITS_PER_PACK = 2
DEFAULT_CREDITS = 3 * CREDITS_PER_PACK  # new alpha tester budget = 3 packs

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
    photo_path     TEXT,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS packs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    character_id INTEGER NOT NULL REFERENCES characters(id),
    owner_id     INTEGER NOT NULL,
    set_name     TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    published    INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS strikes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bans (
    user_id INTEGER PRIMARY KEY,
    until   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS quotas (
    user_id   INTEGER PRIMARY KEY,
    remaining INTEGER NOT NULL
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
        await self._migrate()
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Lightweight migrations for already-created databases."""
        async with self._conn.execute("PRAGMA table_info(packs)") as cur:
            columns = {row["name"] for row in await cur.fetchall()}
        if "published" not in columns:
            # Existing packs were all created at publish time → mark them published.
            await self._conn.execute(
                "ALTER TABLE packs ADD COLUMN published INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.execute("UPDATE packs SET published = 1")
        async with self._conn.execute("PRAGMA table_info(strikes)") as cur:
            strike_cols = {row["name"] for row in await cur.fetchall()}
        if "reason" not in strike_cols:
            await self._conn.execute(
                "ALTER TABLE strikes ADD COLUMN reason TEXT NOT NULL DEFAULT ''"
            )
        async with self._conn.execute("PRAGMA table_info(characters)") as cur:
            char_cols = {row["name"] for row in await cur.fetchall()}
        if "photo_path" not in char_cols:
            # Source photo kept (alpha) so a canonical can be inspected/redrawn.
            await self._conn.execute("ALTER TABLE characters ADD COLUMN photo_path TEXT")
        # Quotas moved from whole packs to half-packs (1 pack = 2 credits): double
        # existing balances once so old testers keep the same number of packs.
        async with self._conn.execute(
            "SELECT value FROM config WHERE key = 'quota_credits_v2'"
        ) as cur:
            migrated = await cur.fetchone()
        if migrated is None:
            await self._conn.execute("UPDATE quotas SET remaining = remaining * 2")
            await self._conn.execute(
                "INSERT INTO config (key, value) VALUES ('quota_credits_v2', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
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
        photo_path: str | None = None,
    ) -> Character:
        """Persist a confirmed canonical character and return it with its id."""
        created = _now()
        cur = await self._conn.execute(
            "INSERT INTO characters "
            "(owner_id, name, style_id, subject_type, child_age, canonical_path, "
            "photo_path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                owner_id,
                name,
                style_id,
                subject_type,
                child_age,
                canonical_path,
                photo_path,
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
            photo_path=photo_path,
            created_at=created,
        )

    async def update_character_canonical(
        self, character_id: int, *, canonical_path: str, photo_path: str | None = None
    ) -> None:
        """Replace a character's canonical (and source photo) after a redraw."""
        await self._conn.execute(
            "UPDATE characters SET canonical_path = ?, photo_path = ? WHERE id = ?",
            (canonical_path, photo_path, character_id),
        )
        await self._conn.commit()

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
        self,
        *,
        character_id: int,
        owner_id: int,
        set_name: str,
        title: str,
        published: bool = False,
    ) -> Pack:
        """Create a pack (draft by default) bound to a character (§3.2)."""
        created = _now()
        cur = await self._conn.execute(
            "INSERT INTO packs (character_id, owner_id, set_name, title, created_at, published) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (character_id, owner_id, set_name, title, created.isoformat(), int(published)),
        )
        await self._conn.commit()
        return Pack(
            id=cur.lastrowid or 0,
            character_id=character_id,
            owner_id=owner_id,
            set_name=set_name,
            title=title,
            created_at=created,
            published=published,
        )

    async def update_pack(
        self, pack_id: int, *, set_name: str | None = None, published: bool | None = None
    ) -> None:
        """Update a pack's set_name and/or published flag (e.g. on publishing a draft)."""
        if set_name is not None:
            await self._conn.execute(
                "UPDATE packs SET set_name = ? WHERE id = ?", (set_name, pack_id)
            )
        if published is not None:
            await self._conn.execute(
                "UPDATE packs SET published = ? WHERE id = ?", (int(published), pack_id)
            )
        await self._conn.commit()

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

    # --- strikes & bans (auto-moderation) ------------------------------------

    async def add_strike(self, user_id: int, reason: str = "") -> int:
        """Record a strike (with reason); return strikes active in the last 30 days."""
        await self._conn.execute(
            "INSERT INTO strikes (user_id, reason, created_at) VALUES (?, ?, ?)",
            (user_id, reason, _now().isoformat()),
        )
        await self._conn.commit()
        return await self.active_strikes(user_id)

    async def list_strikes(self, user_id: int) -> list[tuple[str, str]]:
        """Active (30-day) strikes for a user as (reason, created_at), newest first."""
        cutoff = (_now() - timedelta(days=30)).isoformat()
        async with self._conn.execute(
            "SELECT reason, created_at FROM strikes WHERE user_id = ? AND created_at > ? "
            "ORDER BY created_at DESC",
            (user_id, cutoff),
        ) as cur:
            rows = await cur.fetchall()
        return [(r["reason"], r["created_at"]) for r in rows]

    async def active_strikes(self, user_id: int) -> int:
        """Strikes that have not yet expired (30-day window)."""
        cutoff = (_now() - timedelta(days=30)).isoformat()
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM strikes WHERE user_id = ? AND created_at > ?",
            (user_id, cutoff),
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def set_ban(self, user_id: int, until: datetime) -> None:
        await self._conn.execute(
            "INSERT INTO bans (user_id, until) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET until = excluded.until",
            (user_id, until.isoformat()),
        )
        await self._conn.commit()

    async def banned_until(self, user_id: int) -> datetime | None:
        """Return the ban expiry if the user is currently banned, else None."""
        async with self._conn.execute(
            "SELECT until FROM bans WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        until = datetime.fromisoformat(row["until"])
        return until if until > _now() else None

    async def list_bans(self) -> list[tuple[int, datetime]]:
        """Currently-active bans as (user_id, until)."""
        now = _now().isoformat()
        async with self._conn.execute(
            "SELECT user_id, until FROM bans WHERE until > ? ORDER BY until", (now,)
        ) as cur:
            rows = await cur.fetchall()
        return [(r["user_id"], datetime.fromisoformat(r["until"])) for r in rows]

    async def unban(self, user_id: int) -> None:
        """Lift a ban (admin action)."""
        await self._conn.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    # --- config (key/value) --------------------------------------------------

    async def get_config(self, key: str, default: str = "") -> str:
        async with self._conn.execute("SELECT value FROM config WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_config(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    # --- applications (§alpha) -----------------------------------------------

    async def add_application(self, user_id: int, username: str | None, source: str) -> None:
        """Create or reset an application to 'pending'."""
        await self._conn.execute(
            "INSERT INTO applications (user_id, username, source, created_at, status) "
            "VALUES (?, ?, ?, ?, 'pending') "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
            "source = excluded.source, created_at = excluded.created_at, status = 'pending'",
            (user_id, username, source, _now().isoformat()),
        )
        await self._conn.commit()

    async def get_application(self, user_id: int) -> Application | None:
        async with self._conn.execute(
            "SELECT * FROM applications WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return Application(**dict(row)) if row else None

    async def list_applications(self, status: str) -> list[Application]:
        async with self._conn.execute(
            "SELECT * FROM applications WHERE status = ? ORDER BY created_at", (status,)
        ) as cur:
            rows = await cur.fetchall()
        return [Application(**dict(r)) for r in rows]

    async def set_application_status(self, user_id: int, status: str) -> None:
        await self._conn.execute(
            "UPDATE applications SET status = ? WHERE user_id = ?", (status, user_id)
        )
        await self._conn.commit()

    # --- generation credits (§alpha; stored in half-packs) -------------------

    async def credits_left(self, user_id: int) -> int:
        """Remaining credits in half-packs (1 pack = 2). New users get the default."""
        async with self._conn.execute(
            "SELECT remaining FROM quotas WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["remaining"]) if row else DEFAULT_CREDITS

    async def set_credits(self, user_id: int, remaining: int) -> None:
        remaining = max(0, remaining)
        await self._conn.execute(
            "INSERT INTO quotas (user_id, remaining) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET remaining = excluded.remaining",
            (user_id, remaining),
        )
        await self._conn.commit()

    async def add_credits(self, user_id: int, delta: int) -> int:
        """Add (or subtract) credits in half-packs; clamps at 0; returns new value."""
        new_value = max(0, await self.credits_left(user_id) + delta)
        await self.set_credits(user_id, new_value)
        return new_value

    async def consume_credits(self, user_id: int, amount: int) -> int:
        """Spend ``amount`` credits (half-packs) if available; returns new balance."""
        return await self.add_credits(user_id, -abs(amount))

    # --- analytics events ----------------------------------------------------

    async def add_event(self, user_id: int, event: str, detail: dict[str, object]) -> None:
        """Append an analytics event (JSON detail)."""
        await self._conn.execute(
            "INSERT INTO events (user_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (user_id, event, json.dumps(detail, ensure_ascii=False), _now().isoformat()),
        )
        await self._conn.commit()

    async def has_events(self, user_id: int) -> bool:
        """True if the user has any prior recorded event (returning user)."""
        async with self._conn.execute(
            "SELECT 1 FROM events WHERE user_id = ? LIMIT 1", (user_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def count_events(self, event: str) -> int:
        async with self._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE event = ?", (event,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"]) if row else 0


async def open_database(paths: Sequence[str | Path] | None = None) -> Database:  # pragma: no cover
    """Convenience opener used by the bot runner (covered indirectly)."""
    from sticker_service.config import get_settings

    target = paths[0] if paths else get_settings().data_dir / "sticker_service.sqlite"
    return await Database.connect(target)
