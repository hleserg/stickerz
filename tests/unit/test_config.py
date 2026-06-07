"""Tests for projectname.config."""

from __future__ import annotations

import pytest

from projectname.config import Settings, get_settings


def test_defaults() -> None:
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.environment == "development"
    assert settings.debug is False
    assert settings.sentry_dsn == ""


def test_singleton_is_cached() -> None:
    assert get_settings() is get_settings()


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_DEBUG", "true")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.environment == "production"
    assert settings.debug is True
