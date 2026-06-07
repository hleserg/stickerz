"""Tests for projectname.observability.sentry."""

from __future__ import annotations

from typing import Any

import pytest

from projectname.observability import sentry as obs


def test_init_skipped_without_dsn() -> None:
    assert obs.init_sentry(dsn="") is False


def test_init_with_dsn_sets_privacy_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_init(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("sentry_sdk.init", fake_init)

    assert obs.init_sentry(dsn="https://key@o0.ingest.sentry.io/0", release="1.2.3") is True
    assert captured["send_default_pii"] is False
    assert captured["release"] == "1.2.3"
    assert captured["event_scrubber"] is not None


def test_init_uses_settings_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SENTRY_DSN", "https://key@o0.ingest.sentry.io/0")
    from projectname.config import get_settings

    get_settings.cache_clear()

    called = {"value": False}

    def fake_init(**_kwargs: Any) -> None:
        called["value"] = True

    monkeypatch.setattr("sentry_sdk.init", fake_init)

    assert obs.init_sentry() is True
    assert called["value"] is True


def test_build_scrubber_returns_object() -> None:
    scrubber = obs._build_scrubber()
    assert scrubber is not None


def test_tag_component(monkeypatch: pytest.MonkeyPatch) -> None:
    tags: dict[str, str] = {}

    class FakeScope:
        def set_tag(self, key: str, value: str) -> None:
            tags[key] = value

    monkeypatch.setattr("sentry_sdk.get_current_scope", lambda: FakeScope())

    obs.tag_component("reflection", extra=[("layer", "core")])
    assert tags["component"] == "reflection"
    assert tags["layer"] == "core"
