"""Tests for the image-model interface, mock, factory, and key guards."""

from __future__ import annotations

import pytest

from sticker_service.config import get_settings
from sticker_service.services.models import (
    GeminiImageModel,
    GptImageModel,
    MockImageModel,
    ModelRefusalError,
    build_model,
)


async def test_mock_generate_is_deterministic() -> None:
    model = MockImageModel()
    a = await model.generate("hello", refs=[b"ref"])
    b = await model.generate("hello", refs=[b"ref"])
    c = await model.generate("different", refs=[b"ref"])
    assert a == b  # same prompt+refs -> same bytes
    assert a != c  # different prompt -> different bytes
    assert a.startswith(b"\x89PNG")  # real PNG
    assert model.generate_calls == ["hello", "hello", "different"]


async def test_mock_can_refuse() -> None:
    model = MockImageModel(refuse_on="ребёнок")
    with pytest.raises(ModelRefusalError):
        await model.generate("портрет: ребёнок 6 лет")
    # Non-matching prompt still works.
    assert await model.generate("портрет взрослого")


async def test_mock_judge_and_emoji() -> None:
    model = MockImageModel(judge_score=0.42, emoji="🔥")
    assert await model.judge_geometry(b"a", b"b") == 0.42
    assert await model.pick_emoji(b"img") == "🔥"


def test_build_model_mock() -> None:
    assert isinstance(build_model("mock"), MockImageModel)


def test_build_model_uses_settings_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODEL_PROVIDER", "mock")
    get_settings.cache_clear()
    assert isinstance(build_model(), MockImageModel)


def test_build_model_gemini_and_gpt_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_GEMINI_API_KEY", "k")
    monkeypatch.setenv("APP_OPENAI_API_KEY", "k2")
    get_settings.cache_clear()
    assert build_model("gemini").name == "gemini"
    assert build_model("gpt").name == "gpt"


def test_build_model_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown model provider"):
        build_model("flux")


def test_gemini_requires_key() -> None:
    with pytest.raises(ValueError, match="GEMINI_KEY"):
        GeminiImageModel(api_key="")


def test_gpt_requires_key() -> None:
    with pytest.raises(ValueError, match="GPT_KEY"):
        GptImageModel(api_key="")


def test_build_gemini_and_gpt_with_keys() -> None:
    assert build_model.__name__ == "build_model"  # importable
    assert GeminiImageModel(api_key="k").name == "gemini"
    assert GptImageModel(api_key="k").name == "gpt"


def test_gemini_parse_score() -> None:
    from sticker_service.services.models.gemini import parse_score

    assert parse_score("0.82") == 0.82
    assert parse_score("совпадение: 0.7 примерно") == 0.7
    assert parse_score("1") == 1.0
    assert parse_score("нет числа") == 0.0
    assert parse_score("2.5") == 1.0  # clamped


def test_gemini_parse_emoji() -> None:
    from sticker_service.services.models.gemini import parse_emoji

    assert parse_emoji("🔥") == "🔥"
    assert parse_emoji("вот: 😄 подходит") == "😄"
    assert parse_emoji("no emoji here") is None


def test_gemini_mime_sniff() -> None:
    from sticker_service.services.models.gemini import _mime

    assert _mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert _mime(b"\x89PNG\r\n") == "image/png"


def test_gemini_is_retryable() -> None:
    from sticker_service.services.models.gemini import _is_retryable

    assert _is_retryable(RuntimeError("503 UNAVAILABLE high demand"))
    assert _is_retryable(RuntimeError("429 RESOURCE_EXHAUSTED"))
    assert not _is_retryable(RuntimeError("400 invalid argument"))


def test_gemini_billing_is_not_retryable() -> None:
    # A depleted-credits 429 is permanent: classify as billing, never retry.
    from sticker_service.services.models.gemini import _is_billing, _is_retryable

    depleted = RuntimeError("429 RESOURCE_EXHAUSTED: Your prepayment credits are depleted.")
    assert _is_billing(depleted)
    assert not _is_retryable(depleted)  # billing short-circuits the retry classification
    assert not _is_billing(RuntimeError("429 RESOURCE_EXHAUSTED: rate limit, retry"))


def test_gemini_image_model_fallback_sequence() -> None:
    from sticker_service.services.models.gemini import IMAGE_MODEL, image_model_for_attempt

    assert image_model_for_attempt(0) == IMAGE_MODEL
    assert image_model_for_attempt(1) == IMAGE_MODEL
    assert image_model_for_attempt(2) == "gemini-3.1-flash-image"
    assert image_model_for_attempt(3) == "gemini-2.5-flash-image"
    assert image_model_for_attempt(99) == "gemini-2.5-flash-image"  # clamps to last
