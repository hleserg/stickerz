"""Tests for sticker_service.config."""

from __future__ import annotations

import pytest

from sticker_service.config import Settings, get_settings


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


def test_secret_defaults_empty() -> None:
    settings = get_settings()
    assert settings.bot_token == ""
    assert settings.gemini_api_key == ""
    assert settings.openai_api_key == ""
    assert settings.model_provider == "gemini"
    assert settings.default_gate == "vision_judge"


def test_secrets_read_bare_repo_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # The repo stores secrets without the APP_ prefix (BOT_TOKEN / GEMINI_KEY /
    # GPT_KEY); the AliasChoices must resolve them.
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("GEMINI_KEY", "g-key")
    monkeypatch.setenv("GPT_KEY", "o-key")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.bot_token == "123:abc"
    assert settings.gemini_api_key == "g-key"
    assert settings.openai_api_key == "o-key"


def test_app_prefix_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_BOT_TOKEN", "prefixed")
    monkeypatch.setenv("BOT_TOKEN", "bare")
    get_settings.cache_clear()

    assert get_settings().bot_token == "prefixed"


def test_admin_id_set_parses_and_ignores_blanks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ADMIN_IDS", " 111, 222 ,, 333 ")
    get_settings.cache_clear()

    assert get_settings().admin_id_set == frozenset({111, 222, 333})


def test_admin_id_set_empty_by_default() -> None:
    assert get_settings().admin_id_set == frozenset()
