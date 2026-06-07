"""Sentry integration with privacy-first defaults.

Critical project rule: ``send_default_pii=False``. This codebase may handle
sensitive user data, so PII must never leave the process by default. We layer
the SDK's :class:`EventScrubber` on top and expose a small helper to tag the
emitting component, mirroring the per-component tagging used across the
author's projects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from projectname.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

_EXTRA_DENYLIST: list[str] = [
    "user_text",
    "raw_input",
    "embedding",
    "prompt",
    "completion",
]


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

    import sentry_sdk

    sentry_sdk.init(
        dsn=resolved_dsn,
        environment=settings.sentry_environment,
        release=release if release is not None else (settings.sentry_release or None),
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        event_scrubber=_build_scrubber(),
    )
    return True


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
