"""Sentry integration with privacy-first defaults.

Critical project rule: ``send_default_pii=False``. This codebase may handle
sensitive user data, so PII must never leave the process by default. We layer
the SDK's :class:`EventScrubber` on top and expose a small helper to tag the
emitting component, mirroring the per-component tagging used across the
author's projects.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

from sticker_service.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_EXTRA_DENYLIST: list[str] = [
    "user_text",
    "raw_input",
    "embedding",
    "prompt",
    "completion",
]

# Per-variable cap for serialized frame locals. Relay rejects whole events over
# 1 MiB as invalid:too_large, and a single PNG byte-string local can blow past
# that on its own; 512 chars keeps plenty of debugging context per var.
_MAX_VAR_CHARS = 512


def _placeholder_if_big(value: object) -> object:
    """Return *value*, or a ``<trimmed: N chars>`` placeholder when oversized.

    Containers are handled conservatively: if the JSON-ish repr of the whole
    value exceeds the cap, the whole value is replaced (no partial trimming).
    """
    if isinstance(value, str):
        size = len(value)
    else:
        try:
            size = len(json.dumps(value, default=repr))
        except (TypeError, ValueError):
            size = len(repr(value))
    if size > _MAX_VAR_CHARS:
        return f"<trimmed: {size} chars>"
    return value


def _trim_frames(frames: object) -> None:
    """Replace oversized ``vars`` entries in a list of stacktrace frames."""
    if not isinstance(frames, list):
        return
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        frame_vars = frame.get("vars")
        if not isinstance(frame_vars, dict):
            continue
        for name, value in frame_vars.items():
            try:
                frame_vars[name] = _placeholder_if_big(value)
            except Exception:  # trimming must never break event delivery
                frame_vars[name] = value


# PLAYBOOK-START
# id: sentry-before-send-trim-locals
# title: before_send trims oversized frame locals so events pass ingest limits
# status: draft
# category: observability
# tags: [sentry, before_send, event-size, locals]
# include_local_variables serializes whole frame locals (image bytes, big
# lists) into the event; Relay silently drops events >1 MiB as
# invalid:too_large, so the exact failures you most need never reach Issues.
# A before_send hook that walks exception/threads stacktrace frames and
# replaces any var whose serialized form exceeds a small cap with a
# '<trimmed: N chars>' placeholder keeps events deliverable. The hook must be
# fully defensive (missing keys, wrong shapes) and must never raise — losing
# an event to the trimmer would recreate the original bug.
# PLAYBOOK-END
def _trim_big_locals(event: Any, _hint: Any) -> Any:
    """``before_send`` hook: cap serialized frame locals so events stay small.

    Walks ``exception.values[*].stacktrace.frames[*].vars`` (and the same path
    under ``threads``) defensively — any key may be absent or mis-shaped — and
    never raises: on any internal error the event is returned unchanged.
    """
    try:
        for section in ("exception", "threads"):
            container = event.get(section)
            if not isinstance(container, dict):
                continue
            values = container.get("values")
            if not isinstance(values, list):
                continue
            for entry in values:
                if not isinstance(entry, dict):
                    continue
                stacktrace = entry.get("stacktrace")
                if isinstance(stacktrace, dict):
                    _trim_frames(stacktrace.get("frames"))
    except Exception:  # a before_send hook must never raise
        return event
    return event


def _build_scrubber() -> Any:
    """Build an EventScrubber extending the SDK default denylists."""
    try:
        from sentry_sdk.scrubber import (
            DEFAULT_DENYLIST,
            DEFAULT_PII_DENYLIST,
            EventScrubber,
        )
    except ImportError:  # pragma: no cover - sentry always present in deps
        return None

    return EventScrubber(
        denylist=[*DEFAULT_DENYLIST, *_EXTRA_DENYLIST],
        pii_denylist=[*DEFAULT_PII_DENYLIST, *_EXTRA_DENYLIST],
    )


def init_sentry(*, dsn: str | None = None, release: str | None = None) -> bool:
    """Initialize Sentry with privacy-first defaults.

    Returns ``True`` if Sentry was initialized, ``False`` if skipped (no DSN —
    the safe default for local/dev/CI).

    # PLAYBOOK-START
    # id: sentry-privacy-first-init
    # title: Privacy-first Sentry init (PII off + scrubber + tags)
    # status: refined
    # category: observability
    # tags: [sentry, pii, security, observability]
    # send_default_pii=False is a hard rule; EventScrubber extends the
    # default denylists with project-specific sensitive keys; a DSN-less
    # init is a no-op so dev/CI never ship telemetry.
    # PLAYBOOK-END
    """
    settings = get_settings()
    resolved_dsn = dsn if dsn is not None else settings.sentry_dsn
    if not resolved_dsn:
        return False

    import logging

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=resolved_dsn,
        environment=settings.sentry_environment,
        release=release if release is not None else (settings.sentry_release or None),
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        event_scrubber=_build_scrubber(),
        # Cap serialized frame locals: include_local_variables can embed multi-MB
        # byte strings, and Relay drops events >1 MiB as invalid:too_large.
        before_send=_trim_big_locals,
        # Forward WARNING+ log records to Sentry Logs (errors/warnings show up
        # there too, not just as issues). INFO is deliberately excluded: the
        # bot logs a lot of routine INFO that would flood Sentry and burn quota.
        _experiments={"enable_logs": True},
        integrations=[LoggingIntegration(sentry_logs_level=logging.WARNING)],
    )
    return True


@contextlib.contextmanager
def isolated_scope() -> Iterator[None]:
    """Fork a fresh Sentry scope for the duration of one unit of work.

    The bot runs every update on the same event loop, so without isolation the
    ``component`` (and any other) tag set by :func:`tag_component` lives on the
    shared current scope and bleeds into the next update's events. Wrapping each
    update in this scope means tags set inside are discarded on exit and never
    leak across handlers. No-op when Sentry is not installed.
    """
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - sentry always present in deps
        yield
        return
    with sentry_sdk.isolation_scope():
        yield


def tag_component(component: str, *, extra: Sequence[tuple[str, str]] = ()) -> None:
    """Tag the current Sentry scope with the emitting component name.

    No-op when Sentry is not initialized.
    """
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover
        return

    scope = sentry_sdk.get_current_scope()
    scope.set_tag("component", component)
    for key, value in extra:
        scope.set_tag(key, value)
