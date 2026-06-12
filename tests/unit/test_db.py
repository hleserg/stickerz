"""Tests for the SQLite data-access layer (in-memory)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from sticker_service.db import Database
from sticker_service.db.models import Character


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


async def test_whitelist_allow_deny(db: Database) -> None:
    assert await db.is_allowed(1) is False
    await db.allow(1, "alice")
    assert await db.is_allowed(1) is True
    # Re-allow updates username without duplicating.
    await db.allow(1, "alice2")
    entries = await db.list_whitelist()
    assert len(entries) == 1
    assert entries[0].username == "alice2"
    await db.deny(1)
    assert await db.is_allowed(1) is False


async def test_character_crud_and_reuse(db: Database) -> None:
    char = await db.add_character(
        owner_id=42,
        name="Лёшик",
        style_id="watercolor",
        subject_type="child",
        canonical_path="/data/c1.png",
        child_age=6,
    )
    assert char.id > 0
    fetched = await db.get_character(char.id)
    assert fetched is not None and fetched.name == "Лёшик"

    # One character -> many packs (§3.2).
    p1 = await db.add_pack(
        character_id=char.id, owner_id=42, set_name="leshik_a1_by_bot", title="Лёшик 🎨"
    )
    p2 = await db.add_pack(
        character_id=char.id, owner_id=42, set_name="leshik_b2_by_bot", title="Лёшик 2"
    )
    packs = await db.list_packs(42)
    assert {p.id for p in packs} == {p1.id, p2.id}
    assert all(p.character_id == char.id for p in packs)


async def test_pack_lookups_and_list_characters(db: Database) -> None:
    assert await db.get_character(999) is None
    c1 = await db.add_character(
        owner_id=1, name="A", style_id="watercolor", subject_type="adult", canonical_path="/a"
    )
    c2 = await db.add_character(
        owner_id=1, name="B", style_id="watercolor", subject_type="adult", canonical_path="/b"
    )
    chars = await db.list_characters(1)
    assert {c.id for c in chars} == {c1.id, c2.id}

    pack = await db.add_pack(character_id=c1.id, owner_id=1, set_name="look_by_bot", title="t")
    assert (await db.get_pack(pack.id)).id == pack.id  # type: ignore[union-attr]
    assert (await db.get_pack_by_set_name("look_by_bot")).id == pack.id  # type: ignore[union-attr]
    assert await db.get_pack(123456) is None
    assert await db.get_pack_by_set_name("nope") is None


async def test_set_name_unique(db: Database) -> None:
    char = await db.add_character(
        owner_id=1, name="A", style_id="watercolor", subject_type="adult", canonical_path="/x"
    )
    await db.add_pack(character_id=char.id, owner_id=1, set_name="dup_by_bot", title="t")
    with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError on UNIQUE
        await db.add_pack(character_id=char.id, owner_id=1, set_name="dup_by_bot", title="t2")


async def test_stickers(db: Database) -> None:
    char = await db.add_character(
        owner_id=1, name="A", style_id="watercolor", subject_type="adult", canonical_path="/x"
    )
    pack = await db.add_pack(character_id=char.id, owner_id=1, set_name="s_by_bot", title="t")
    await db.add_sticker(pack_id=pack.id, file_path="/a.png", emoji="🙂", position=0)
    await db.add_sticker(pack_id=pack.id, file_path="/b.png", emoji="👍", position=1)
    assert await db.count_stickers(pack.id) == 2
    stickers = await db.list_stickers(pack.id)
    assert [s.position for s in stickers] == [0, 1]


async def test_order_roundtrip(db: Database) -> None:
    assert await db.get_order(7) is None
    await db.save_order(7, {"step": "await_photo", "style_id": None})
    order = await db.get_order(7)
    assert order is not None and order.state["step"] == "await_photo"
    # Upsert overwrites.
    await db.save_order(7, {"step": "choose_style"})
    order2 = await db.get_order(7)
    assert order2 is not None and order2.state["step"] == "choose_style"
    await db.clear_order(7)
    assert await db.get_order(7) is None


async def test_consent(db: Database) -> None:
    assert await db.has_consent(5) is False
    await db.record_consent(5)
    assert await db.has_consent(5) is True


def test_child_requires_age() -> None:
    with pytest.raises(ValueError, match="child_age"):
        Character(
            id=1,
            owner_id=1,
            name="x",
            style_id="watercolor",
            subject_type="child",
            child_age=None,
            canonical_path="/x",
            created_at="2026-01-01T00:00:00+00:00",  # type: ignore[arg-type]
        )


def test_adult_forbids_age() -> None:
    with pytest.raises(ValueError, match="child_age"):
        Character(
            id=1,
            owner_id=1,
            name="x",
            style_id="watercolor",
            subject_type="adult",
            child_age=5,
            canonical_path="/x",
            created_at="2026-01-01T00:00:00+00:00",  # type: ignore[arg-type]
        )


async def test_sticker_caption_roundtrip(db: Database) -> None:
    # History feature (12.06): a sticker remembers the idea it was drawn for;
    # rows without a caption read back as None.
    char = await db.add_character(
        owner_id=1, name="A", style_id="watercolor", subject_type="adult", canonical_path="/x"
    )
    pack = await db.add_pack(character_id=char.id, owner_id=1, set_name="cap_by_bot", title="t")
    await db.add_sticker(
        pack_id=pack.id, file_path="/a.png", emoji="🙂", position=0, caption="Привет!"
    )
    await db.add_sticker(pack_id=pack.id, file_path="/b.png", emoji="👍", position=1)
    stickers = await db.list_stickers(pack.id)
    assert [s.caption for s in stickers] == ["Привет!", None]


async def test_caption_column_migrates_old_db(tmp_path: object) -> None:
    # A pre-12.06 database (stickers table without the caption column) must
    # gain it transparently on connect.
    import aiosqlite

    path = tmp_path / "old.sqlite"  # type: ignore[operator]
    conn = await aiosqlite.connect(path)
    await conn.executescript(
        "CREATE TABLE stickers ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, pack_id INTEGER NOT NULL,"
        " file_path TEXT NOT NULL, emoji TEXT NOT NULL, position INTEGER NOT NULL,"
        " created_at TEXT NOT NULL);"
    )
    await conn.commit()
    await conn.close()
    database = await Database.connect(path)
    try:
        sticker = await database.add_sticker(
            pack_id=1, file_path="/a.png", emoji="🙂", position=0, caption="Огонь!"
        )
        assert sticker.caption == "Огонь!"
    finally:
        await database.close()


async def test_events_for_returns_recent_parsed(db: Database) -> None:
    for i in range(7):
        await db.add_event(1, "captions_selected", {"total": i})
    await db.add_event(2, "captions_selected", {"total": 99})  # other user — excluded
    rows = await db.events_for(1, "captions_selected", limit=5)
    assert len(rows) == 5
    assert [d["total"] for _, d in rows] == [6, 5, 4, 3, 2]  # newest first
