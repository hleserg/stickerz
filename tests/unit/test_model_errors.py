"""Tests for the unified model-error taxonomy (classify + user message + policy)."""

from __future__ import annotations

from sticker_service.services.models import errors
from sticker_service.services.models.base import ModelQuotaError, ModelRefusalError


def test_classify_quota_by_type_and_text() -> None:
    assert errors.classify(ModelQuotaError("x")) == errors.QUOTA
    assert (
        errors.classify(RuntimeError("429: Your prepayment credits are depleted")) == errors.QUOTA
    )
    assert errors.is_quota(RuntimeError("billing account not found"))


def test_classify_refusal() -> None:
    assert errors.classify(ModelRefusalError("blocked")) == errors.REFUSAL
    assert errors.classify(RuntimeError("finish_reason: SAFETY")) == errors.REFUSAL


def test_classify_transient_vs_quota_429() -> None:
    # A plain rate-limit 429 is transient (retry); a credits 429 is quota (don't).
    assert errors.classify(RuntimeError("429 RESOURCE_EXHAUSTED rate limit")) == errors.TRANSIENT
    assert errors.is_retryable(RuntimeError("503 high demand"))
    assert not errors.is_retryable(RuntimeError("429: prepayment credits are depleted"))


def test_classify_network_and_unknown() -> None:
    assert errors.classify(RuntimeError("connection timeout to proxy")) == errors.NETWORK
    assert errors.classify(RuntimeError("400 invalid argument")) == errors.UNKNOWN


def test_connection_refused_is_network_not_refusal() -> None:
    # "connection refused" must read as NETWORK (connectivity), not a
    # content-filter refusal that tells the user to change the photo.
    assert errors.classify(OSError("[Errno 111] Connection refused")) == errors.NETWORK


def test_http_codes_match_on_word_boundary() -> None:
    # A bare code is transient; the same digits inside a larger token are not.
    assert errors.classify(RuntimeError("503 Service Unavailable")) == errors.TRANSIENT
    assert errors.classify(RuntimeError("request id req-50012 failed")) == errors.UNKNOWN
    assert errors.classify(RuntimeError("order 4290 not found")) == errors.UNKNOWN


def test_timeout_is_retryable_transient() -> None:
    # A generation timeout (empty str()) must be retryable so the user gets the
    # retry button, not a dead-end. asyncio.TimeoutError is builtin TimeoutError.
    assert errors.classify(TimeoutError()) == errors.TRANSIENT
    assert errors.is_retryable(TimeoutError())


def test_user_message_per_kind() -> None:
    assert "лимит" in errors.user_message(ModelQuotaError("x"))
    assert "перегружена" in errors.user_message(RuntimeError("503 high demand"))
    # Unknown errors are GENERIC for users (internals live in logs/Sentry):
    # a live tester once saw "fresh mode needs photo, style_id and subject_type".
    unknown = errors.user_message(RuntimeError("400 invalid argument"))
    assert "400" not in unknown and "/new" in unknown


def test_pipeline_error_is_retryable_with_apology() -> None:
    # A broken sheet must surface as TRANSIENT: the user gets the apology and
    # the free retry button instead of a raw error (owner's rule: garbage
    # never ships, retry costs nothing).
    from sticker_service.services.models.errors import (
        TransientPipelineError,
        is_retryable,
        user_message,
    )

    exc = TransientPipelineError("sheet slicing failed: scrap piece")
    assert is_retryable(exc) is True
    assert "бесплатно" in user_message(exc)
