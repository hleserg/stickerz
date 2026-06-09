"""Tests for caption moderation (profanity / nudity / gore)."""

from __future__ import annotations

import pytest

from sticker_service.services.moderation import caption_rejection_reason, is_clean


@pytest.mark.parametrize(
    "bad",
    [
        "хуй",
        "иди нахуй",
        "ебать",
        "блять",
        "голая девушка",
        "кровь и кишки",
        "fuck you",
        "porn",
        "sex",
        "хуууй",  # collapsed repeats
        "п и з д а",  # spaces stripped
        "xуйня",  # latin x homoglyph
        "Г0Л0Е",  # 0 -> о homoglyph
        "х​уй",  # zero-width space inside the stem
        "‮хуй‬",  # RTL override wrapping
        "се­кс",  # soft hyphen split
    ],
)
def test_blocks_bad_captions(bad: str) -> None:
    # The invisible-character cases pass because the normalizers whitelist
    # letters and DROP everything else — a property worth pinning forever.
    assert not is_clean(bad)
    assert caption_rejection_reason(bad) is not None


@pytest.mark.parametrize(
    "good",
    [
        "Привет!",
        "Я крутой",
        "Хлеб",
        "Корабль плывёт",
        "Спасибо большое",
        "Какао!",
        "Не в садик!",
        "Ха-ха-ха",
        "",
    ],
)
def test_allows_clean_captions(good: str) -> None:
    assert is_clean(good)
    assert caption_rejection_reason(good) is None
