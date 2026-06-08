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
