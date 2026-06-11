"""Tests for bot operating modes."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio

from sticker_service.db import Database
from sticker_service.services import modes


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def test_implemented_modes() -> None:
    # Only debug and alpha are implemented so far; prod/beta cannot be switched to.
    assert modes.is_implemented(modes.DEBUG)
    assert modes.is_implemented(modes.ALPHA)
    assert not modes.is_implemented(modes.PROD)
    assert not modes.is_implemented(modes.BETA)


async def test_get_default_then_set(db: Database) -> None:
    assert await modes.get_mode(db) == modes.DEFAULT
    await modes.set_mode(db, modes.PROD)
    assert await modes.get_mode(db) == modes.PROD


async def test_alpha_start_stamped_once(db: Database) -> None:
    # Switching INTO alpha stamps when it opened; later toggles keep the stamp,
    # so the stats/budget window survives a maintenance debug→alpha roundtrip.
    assert await modes.alpha_started_at(db) is None
    await modes.set_mode(db, modes.ALPHA)
    started = await modes.alpha_started_at(db)
    assert started is not None
    await modes.set_mode(db, modes.DEBUG)
    await modes.set_mode(db, modes.ALPHA)
    assert await modes.alpha_started_at(db) == started  # unchanged
