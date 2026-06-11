"""Tests for the persistent SQLite FSM storage (survives restarts; handles bytes)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio
from aiogram.fsm.storage.base import StorageKey

from sticker_service.fsm_storage import SqliteStorage


def _key(user_id: int = 1) -> StorageKey:
    return StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)


@pytest_asyncio.fixture
async def storage(tmp_path: Path) -> AsyncIterator[SqliteStorage]:
    store = await SqliteStorage.create(tmp_path / "fsm.sqlite")
    try:
        yield store
    finally:
        await store.close()


async def test_state_round_trip(storage: SqliteStorage) -> None:
    key = _key()
    assert await storage.get_state(key) is None  # nothing stored yet
    await storage.set_state(key, "NewPack:name")
    assert await storage.get_state(key) == "NewPack:name"
    await storage.set_state(key, None)  # clearing
    assert await storage.get_state(key) is None


async def test_data_round_trip_with_bytes(storage: SqliteStorage) -> None:
    key = _key()
    assert await storage.get_data(key) == {}  # default empty
    payload = {"name": "Лёшик", "child_age": 6, "photo": b"\x89PNG\x00raw"}
    await storage.set_data(key, payload)
    restored = await storage.get_data(key)
    assert restored["name"] == "Лёшик"
    assert restored["child_age"] == 6
    assert restored["photo"] == b"\x89PNG\x00raw"  # bytes survive the JSON round-trip


async def test_state_and_data_are_independent(storage: SqliteStorage) -> None:
    key = _key()
    await storage.set_state(key, "NewPack:style")
    await storage.set_data(key, {"k": "v"})
    # writing data must not wipe the state (and vice-versa)
    assert await storage.get_state(key) == "NewPack:style"
    await storage.set_state(key, "NewPack:review")
    assert await storage.get_data(key) == {"k": "v"}


async def test_update_data_merges(storage: SqliteStorage) -> None:
    key = _key()
    await storage.set_data(key, {"a": 1})
    merged = await storage.update_data(key, {"b": 2})
    assert merged == {"a": 1, "b": 2}
    assert await storage.get_data(key) == {"a": 1, "b": 2}


async def test_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "fsm.sqlite"
    key = _key(42)
    store = await SqliteStorage.create(path)
    await store.set_state(key, "NewPack:name")
    await store.set_data(key, {"photo": b"img", "name": "Аня"})
    await store.close()  # simulate an OOM/restart wiping the process

    reopened = await SqliteStorage.create(path)
    try:
        assert await reopened.get_state(key) == "NewPack:name"  # flow resumes
        assert await reopened.get_data(key) == {"photo": b"img", "name": "Аня"}
    finally:
        await reopened.close()


# --- sweep_stale: the file must not grow forever on a long-lived volume -------


async def test_sweep_drops_cleared_rows_keeps_active(storage: SqliteStorage) -> None:
    cleared, active = _key(1), _key(2)
    # A finished flow: aiogram's state.clear() leaves state=None and data={}.
    await storage.set_state(cleared, "NewPack:publish")
    await storage.set_data(cleared, {"x": 1})
    await storage.set_state(cleared, None)
    await storage.set_data(cleared, {})
    # A live flow mid-wizard.
    await storage.set_state(active, "NewPack:review")
    await storage.set_data(active, {"photo": b"img"})

    removed = await storage.sweep_stale(older_than_days=14)
    assert removed == 1
    assert await storage.get_state(active) == "NewPack:review"
    assert await storage.get_data(active) == {"photo": b"img"}


async def test_sweep_drops_abandoned_flows_past_retention(storage: SqliteStorage) -> None:
    import time

    abandoned = _key(3)
    await storage.set_state(abandoned, "NewPack:photo")
    await storage.set_data(abandoned, {"photo": b"big"})
    # Backdate the row as if the user walked away a month ago.
    conn = storage._conn  # no public time-travel API, by design
    await conn.execute("UPDATE fsm SET updated_at=?", (time.time() - 30 * 86400,))
    await conn.commit()

    assert await storage.sweep_stale(older_than_days=14) == 1
    assert await storage.get_state(abandoned) is None
    # Retention off (0): abandoned flows are kept, only cleared rows go.
    await storage.set_state(abandoned, "NewPack:photo")
    assert await storage.sweep_stale(older_than_days=0) == 0
    assert await storage.get_state(abandoned) == "NewPack:photo"


async def test_migration_stamps_existing_rows(tmp_path: Path) -> None:
    import aiosqlite

    # A pre-updated_at database, as deployed before this change.
    path = tmp_path / "fsm.sqlite"
    conn = await aiosqlite.connect(path)
    await conn.execute("CREATE TABLE fsm (key TEXT PRIMARY KEY, state TEXT, data TEXT)")
    await conn.execute("INSERT INTO fsm VALUES ('1:1:1:None:None:default', 'S', '{\"a\": 1}')")
    await conn.commit()
    await conn.close()

    store = await SqliteStorage.create(path)
    try:
        # The migrated in-flight row is stamped "now", so it survives the sweep.
        assert await store.sweep_stale(older_than_days=14) == 0
        assert await store.get_state(_key(1)) == "S"
    finally:
        await store.close()


async def test_keys_in_state_returns_parsed_ids(storage: SqliteStorage) -> None:
    # The boot path uses this to find flows orphaned mid-generation.
    await storage.set_state(_key(7), "NewPack:publish")
    await storage.set_state(_key(8), "NewPack:review")
    assert await storage.keys_in_state("NewPack:publish") == [(1, 7, 7)]
    assert await storage.keys_in_state("NoSuch:state") == []


async def test_revive_orphaned_generations(storage: SqliteStorage) -> None:
    # A user stuck in `publish` after a hard restart gets a retry button and is
    # moved back to review; the owner gets a heads-up DM.
    from sticker_service.config import get_settings
    from sticker_service.handlers.flow import NewPack, revive_orphaned_generations

    await storage.set_state(_key(7), NewPack.publish.state)

    sent: list[tuple[int, str]] = []

    class _Bot:
        async def send_message(self, chat_id: int, text: str, **kw: object) -> None:
            sent.append((chat_id, text))

    revived = await revive_orphaned_generations(_Bot(), storage)
    assert revived == 1
    assert await storage.get_state(_key(7)) == NewPack.review.state  # un-stuck
    chats = [c for c, _ in sent]
    assert 7 in chats  # the user got the resume offer
    owner = get_settings().first_admin_id
    if owner is not None:  # the owner is informed about the orphan
        assert owner in chats
