"""Caption fidelity gate: quote-rule expectations, fuzzy judging, vision parsing."""

from __future__ import annotations

import pytest

from sticker_service.services.models.mock import MockImageModel
from sticker_service.services.stickers.caption_check import (
    expected_caption,
    expected_captions,
    judge_captions,
    read_sheet_texts,
)


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("Привет!", "Привет!"),  # standard replica maps through STANDARD_PROMPTS
        ("Класс!", "Класс!"),
        ("Окей 😉", "Ок!"),
        ("Грустно", None),  # emotion: bare word, nothing to write
        ("Я крутой", None),  # unquoted in STANDARD_PROMPTS — draw-only per the contract
        ('"Доброе утро мамочка!"', "Доброе утро мамочка!"),
        ('В длинном пальто "Эй, стикеров хочешь"?', "Эй, стикеров хочешь"),
        ("„Добби свободен“", "Добби свободен"),
        ("Просто сценка без кавычек", None),
        ('""', None),  # empty quotes are not a caption
    ],
)
def test_expected_caption_follows_the_quote_rule(item: str, expected: str | None) -> None:
    assert expected_caption(item) == expected


def test_expected_captions_keeps_order_and_skips_textless() -> None:
    items = ["Привет!", "Грустно", '"Огонь!"', "сценка без текста"]
    assert expected_captions(items) == ["Привет!", "Огонь!"]


def test_judge_passes_a_faithful_sheet() -> None:
    verdict = judge_captions(["Привет!", "ОГОНЬ"], ["Привет!", "Огонь!"])
    assert verdict.ok
    assert verdict.reason == "ok"


def test_judge_catches_missing_and_duplicated() -> None:
    # The live 2026-06-12 pack: «Доброе утро мамочка!» drawn twice plus the
    # «Доброе!» scrap, «Спокойной ночи котёночек» never drawn at all.
    expected = ["Доброе утро мамочка!", "Спокойной ночи котёночек ❤️"]
    drawn = ["Доброе утро мамочка!", "Доброе утро мамочка!", "Доброе!"]
    verdict = judge_captions(drawn, expected)
    assert not verdict.ok
    assert verdict.missing == ("Спокойной ночи котёночек ❤️",)
    assert "Доброе утро мамочка!" in verdict.duplicated
    assert "пропали" in verdict.reason
    assert "задублились" in verdict.reason


def test_judge_tolerates_ocr_noise() -> None:
    # Case, ё/е and punctuation drift must not burn a paid regeneration.
    verdict = judge_captions(
        ["ник@я не понял но очень интересно"], ["Ник@я не понял, но очень интересно"]
    )
    assert verdict.ok


def test_judge_reports_extras_without_failing() -> None:
    verdict = judge_captions(["Привет!", "Совершенно лишний текст"], ["Привет!"])
    assert verdict.ok
    assert verdict.extra == ("Совершенно лишний текст",)
    assert "лишние" in verdict.reason


async def test_read_sheet_texts_parses_lines_and_sentinel() -> None:
    model = MockImageModel(ask_answer="- Привет!\n• Пока!\n")
    assert await read_sheet_texts(model, b"PNG") == ["Привет!", "Пока!"]
    assert await read_sheet_texts(MockImageModel(ask_answer="НЕТ"), b"PNG") == []
    assert await read_sheet_texts(MockImageModel(ask_answer=""), b"PNG") is None


async def test_read_sheet_texts_fails_open_on_vision_error() -> None:
    class _Boom(MockImageModel):
        async def ask(self, image: bytes, question: str) -> str:
            raise RuntimeError("vision down")

    assert await read_sheet_texts(_Boom(), b"PNG") is None
