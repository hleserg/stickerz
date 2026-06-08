"""Tests for the photo foolproof-check classifier."""

from __future__ import annotations

import pytest

from sticker_service.services import photo_check


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("person=yes, big=yes, nude=no, single=yes", None),
        ("person=yes, big=yes, nude=yes, single=yes", photo_check.NUDE),
        ("person=no, big=no, nude=no, single=no", photo_check.NO_PERSON),
        ("person=yes, big=yes, nude=no, single=no", photo_check.MULTI),
        ("person=yes, big=no, nude=no, single=yes", photo_check.SMALL),
        ("person=да, big=да, nude=нет, single=да", None),  # russian yes/no
        ("", None),  # ambiguous → lenient pass
        ("не знаю", None),
    ],
)
def test_classify(answer: str, expected: str | None) -> None:
    assert photo_check.classify(answer) == expected


def test_nude_takes_priority() -> None:
    # Nude wins even if other flags also fail.
    assert photo_check.classify("person=no, nude=yes, single=no, big=no") == photo_check.NUDE
