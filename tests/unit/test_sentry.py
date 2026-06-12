"""Tests for sticker_service.observability.sentry."""

from __future__ import annotations

import json
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
    # Frame locals are capped so events never exceed Relay's 1 MiB limit.
    assert captured["before_send"] is obs._trim_big_locals
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


def test_before_send_trims_oversized_frame_vars() -> None:
    """Regression: a 4 MB local must not balloon the event past Relay's 1 MiB cap.

    Production generation failures carried the whole PNG ``sheet`` bytes and the
    ``sliced`` list in frame vars (~5 MB events), which Relay silently dropped
    as invalid:too_large — the failures never reached Issues.
    """
    big_sheet = "b'\\x89PNG" + "x" * 4_000_000 + "'"
    big_sliced = [repr(b"y" * 1_200) for _ in range(9)]
    event = {
        "exception": {
            "values": [
                {
                    "type": "TransientPipelineError",
                    "stacktrace": {
                        "frames": [
                            {
                                "function": "orchestrate",
                                "vars": {
                                    "sheet": big_sheet,
                                    "sliced": big_sliced,
                                    "reason": "scrap piece (min/median area 0.04)",
                                    "attempt": 2,
                                },
                            }
                        ]
                    },
                }
            ]
        },
        "threads": {"values": [{"stacktrace": {"frames": [{"vars": {"buf": big_sheet}}]}}]},
    }

    out = obs._trim_big_locals(event, {})

    assert out is event
    frame_vars = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert frame_vars["sheet"] == f"<trimmed: {len(big_sheet)} chars>"
    assert frame_vars["sliced"].startswith("<trimmed: ")
    thread_vars = out["threads"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert thread_vars["buf"].startswith("<trimmed: ")
    assert len(json.dumps(out)) < 100_000


def test_before_send_keeps_small_vars_unchanged() -> None:
    """Short strings, ints and small containers must pass through untouched."""
    small_vars = {
        "reason": "scrap piece",
        "attempt": 2,
        "ratio": 0.04,
        "flags": [1, 2, 3],
        "meta": {"path": "components"},
        "none": None,
    }
    event = {"exception": {"values": [{"stacktrace": {"frames": [{"vars": dict(small_vars)}]}}]}}

    out = obs._trim_big_locals(event, {})

    assert out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"] == small_vars


def test_before_send_tolerates_malformed_events() -> None:
    """The hook must never raise — a crash here would lose the event entirely."""
    malformed: list[Any] = [
        {},
        {"exception": "not-a-dict"},
        {"exception": {"values": "not-a-list"}},
        {"exception": {"values": [None, "str", {"stacktrace": None}]}},
        {"exception": {"values": [{"stacktrace": {"frames": None}}]}},
        {"exception": {"values": [{"stacktrace": {"frames": [None, {"vars": "x"}]}}]}},
        {"threads": ["not-a-dict-container"]},
    ]
    for event in malformed:
        assert obs._trim_big_locals(event, {}) is event
    # Even a non-dict "event" comes back unchanged instead of raising.
    assert obs._trim_big_locals(None, {}) is None


def test_init_sentry_caps_error_event_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: a logger.exception with a 4 MB local yields a < 100 KB event."""
    import logging

    import sentry_sdk
    from sentry_sdk.transport import Transport

    captured: list[Any] = []

    class CapturingTransport(Transport):
        def capture_envelope(self, envelope: Any) -> None:
            captured.append(envelope)

        def flush(self, timeout: Any, callback: Any = None) -> None:
            pass

        def kill(self) -> None:
            pass

    real_init = sentry_sdk.init

    def init_with_capture(**kwargs: Any) -> Any:
        kwargs["transport"] = CapturingTransport()
        return real_init(**kwargs)

    monkeypatch.setattr("sentry_sdk.init", init_with_capture)

    def orchestrate() -> None:
        sheet = b"\x89PNG" + b"x" * 4_000_000  # noqa: F841 - big local on purpose
        sliced = [b"y" * 120_000 for _ in range(9)]  # noqa: F841
        raise RuntimeError("sheet slicing failed: scrap piece")

    try:
        assert obs.init_sentry(dsn="https://0@o0.ingest.sentry.io/0", release="test") is True
        logger = logging.getLogger("test_sentry.event_size")
        with sentry_sdk.isolation_scope():
            try:
                orchestrate()
            except RuntimeError:
                logger.exception("generation failed")
        sentry_sdk.flush(timeout=5)
    finally:
        # Detach the client so the global Sentry state does not leak into
        # other tests once monkeypatch restores the real init.
        sentry_sdk.get_global_scope().set_client(None)

    event_payloads = [
        item.payload.get_bytes()
        for envelope in captured
        for item in envelope.items
        if item.type == "event"
    ]
    assert event_payloads, "the error must still produce an event envelope item"
    for payload in event_payloads:
        assert len(payload) < 100_000
        assert b"<trimmed: " in payload
