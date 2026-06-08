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


def test_user_message_per_kind() -> None:
    assert "лимит" in errors.user_message(ModelQuotaError("x"))
    assert "перегружена" in errors.user_message(RuntimeError("503 high demand"))
    # Unknown errors surface their own text so admins can diagnose.
    assert "400 invalid argument" in errors.user_message(RuntimeError("400 invalid argument"))
