"""Tests for one-call sheet generation, caption sets, and refusal retries."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from sticker_service.services.canonical.schema import Style
from sticker_service.services.models.base import ImageModel, ModelRefusalError
from sticker_service.services.stickers import (
    CHROMA,
    SheetRefusedError,
    build_caption_set,
    build_sheet_prompt,
    generate_sheet,
)
from sticker_service.services.stickers.sets import STANDARD_BLOCK


def _style() -> Style:
    return Style.model_validate(
        {
            "schema_version": 1,
            "style_id": "watercolor",
            "display_name": "Акварель",
            "distance": "far",
            "pipeline": [{"step": 1, "prompt": "p {age_clause}", "refs": ["photo"]}],
            "sticker_style_suffix": "watercolor style {age_clause}",
        }
    )


class _RefuserModel(ImageModel):
    """Refuses the first ``refuse_times`` generate calls, then succeeds."""

    name = "refuser"

    def __init__(self, refuse_times: int) -> None:
        self._left = refuse_times
        self.calls: list[str] = []

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:
        self.calls.append(prompt)
        if self._left > 0:
            self._left -= 1
            raise ModelRefusalError("safety")
        return b"\x89PNGsheet"

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:
        return 1.0

    async def pick_emoji(self, image: bytes) -> str:
        return "🙂"


# --- caption sets ------------------------------------------------------------


def test_build_caption_set_standard_only() -> None:
    captions = build_caption_set()
    assert captions == list(STANDARD_BLOCK)


def test_selected_captions_merges_sorted_and_caps() -> None:
    from sticker_service.services.stickers import selected_captions
    from sticker_service.services.stickers.sets import STANDARD_BLOCK

    out = selected_captions([2, 0, 0, 99], ["Своё1", "Своё2"])
    assert out == [STANDARD_BLOCK[0], STANDARD_BLOCK[2], "Своё1", "Своё2"]  # sorted, deduped
    # caps at 24
    big = selected_captions(list(range(len(STANDARD_BLOCK))), [f"c{i}" for i in range(30)])
    assert len(big) == 24


def test_build_caption_set_with_personal_and_limit() -> None:
    captions = build_caption_set(personal=["Не в садик!", "Какао!", "Хм...", "Класс!"], limit=15)
    assert len(captions) == 15
    assert "Не в садик!" in captions


# --- prompt ------------------------------------------------------------------


def test_sheet_prompt_has_chroma_captions_and_resolved_suffix() -> None:
    captions = ["Привет!", "Класс!"]
    prompt = build_sheet_prompt(_style(), captions, age_clause="")
    assert CHROMA in prompt
    assert '"Привет!"' in prompt and '"Класс!"' in prompt
    assert "watercolor style" in prompt
    assert "{age_clause}" not in prompt  # placeholder resolved


def test_sheet_prompt_child_age_clause() -> None:
    prompt = build_sheet_prompt(_style(), ["Привет!"], age_clause="Это ребёнок примерно 6 лет: ...")
    assert "ребёнок" in prompt


# --- generation --------------------------------------------------------------


async def test_generate_sheet_single_call() -> None:
    from sticker_service.services.models import MockImageModel

    model = MockImageModel()
    sheet = await generate_sheet(
        model, b"CANON", _style(), build_caption_set(), subject_type="adult"
    )
    assert sheet.startswith(b"\x89PNG")
    assert len(model.generate_calls) == 1  # ONE call for the whole sheet (§B.4)


async def test_generate_sheet_retries_then_succeeds() -> None:
    model = _RefuserModel(refuse_times=2)
    sheet = await generate_sheet(
        model, b"CANON", _style(), ["Привет!"], subject_type="child", child_age=6
    )
    assert sheet == b"\x89PNGsheet"
    assert len(model.calls) == 3  # 2 refusals + 1 success


async def test_generate_sheet_gives_up_after_retries() -> None:
    model = _RefuserModel(refuse_times=5)
    with pytest.raises(SheetRefusedError):
        await generate_sheet(
            model, b"CANON", _style(), ["Привет!"], subject_type="adult", max_refusal_retries=3
        )
    assert len(model.calls) == 3
