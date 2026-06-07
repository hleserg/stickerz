"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from sticker_service.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give each test a fresh Settings that ignores any local ``.env``.

    A developer's ``.env`` (real bot token / API keys) must not leak into the
    suite — tests stay deterministic and assert on declared defaults.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
