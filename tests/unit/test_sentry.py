"""Tests for sticker_service.observability.sentry."""

from __future__ import annotations

from typing import Any

import pytest

from sticker_service.observability import sentry as obs


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
    # Sentry Logs on; WARNING+ Python logs forwarded (INFO excluded to save quota).
    assert captured["_experiments"]["enable_logs"] is True
    from sentry_sdk.integrations.logging import LoggingIntegration

    log_ints = [i for i in captured["integrations"] if isinstance(i, LoggingIntegration)]
    assert log_ints, "a LoggingIntegration must be configured for Sentry Logs"


def test_init_uses_settings_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_SENTRY_DSN", "https://key@o0.ingest.sentry.io/0")
    from sticker_service.config import get_settings

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


def test_isolated_scope_discards_tags_on_exit() -> None:
    """A tag set inside the per-update scope must not leak to the next update."""
    import sentry_sdk

    def tags_of(scope: Any) -> dict[str, str]:
        return dict(getattr(scope, "_tags", {}))

    before = tags_of(sentry_sdk.get_current_scope())
    with obs.isolated_scope():
        obs.tag_component("handlers.flow")
        assert tags_of(sentry_sdk.get_current_scope())["component"] == "handlers.flow"
    after = tags_of(sentry_sdk.get_current_scope())
    assert after == before  # the per-update tag was discarded, not carried forward
