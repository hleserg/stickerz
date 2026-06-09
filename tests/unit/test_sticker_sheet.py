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

    async def generate(self, prompt: str, refs: Sequence[bytes] = (), **_: object) -> bytes:
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
    # caps at MAX_CAPTIONS (one sheet)
    from sticker_service.services.stickers.sets import MAX_CAPTIONS

    big = selected_captions(list(range(len(STANDARD_BLOCK))), [f"c{i}" for i in range(30)])
    assert len(big) == MAX_CAPTIONS


def test_build_caption_set_with_personal_and_limit() -> None:
    captions = build_caption_set(personal=["Не в садик!", "Какао!", "Хм...", "Класс!"], limit=15)
    assert len(captions) == 15
    assert "Не в садик!" in captions


# --- prompt ------------------------------------------------------------------


def test_sheet_prompt_turns_standard_reactions_into_drawn_ideas() -> None:
    captions = ["Привет!", "Класс!"]
    prompt = build_sheet_prompt(_style(), captions, age_clause="")
    assert CHROMA in prompt
    # Standard reactions become unquoted scene descriptions: the emotion is
    # drawn, never captioned (a quoted label would order the model to letter it).
    assert '"Привет!"' not in prompt and '"Класс!"' not in prompt
    assert "машет рукой в знак приветствия" in prompt
    assert "большой палец вверх" in prompt
    assert "watercolor style" in prompt
    assert "{age_clause}" not in prompt  # placeholder resolved
    # Freedom-first brief: no text unless asked, emotion in the drawing, free
    # composition, caption placed naturally; caption-only ideas get acted out.
    assert "NO text unless the idea explicitly asks" in prompt
    assert "Show the emotion in the drawing" in prompt
    assert "yours to invent" in prompt
    assert "not as a banner pinned to the bottom" in prompt
    assert "act it out" in prompt
    assert "die-cut" in prompt
    # The old hard "caption ON the figure" rule is gone.
    assert "directly ON the character" not in prompt


def test_every_standard_reaction_has_a_quote_free_drawn_idea() -> None:
    from sticker_service.services.stickers.sets import STANDARD_BLOCK, STANDARD_IDEAS

    assert set(STANDARD_IDEAS) == set(STANDARD_BLOCK)
    # No quote characters inside ideas — quotes would order the model to letter text.
    assert all('"' not in v and "«" not in v for v in STANDARD_IDEAS.values())


def test_sheet_prompt_stays_lean() -> None:
    # The brief must not creep back into a micromanaging wall of text: the
    # scaffold around the ideas list stays under a hard budget.
    prompt = build_sheet_prompt(_style(), ["Привет!"], age_clause="")
    assert len(prompt) < 1400


def test_sheet_prompt_pins_ideas_to_tiles_and_text_to_its_tile() -> None:
    # Regression for the «Я проснулся. Технически.» sheet: one idea's caption
    # bled under a NEIGHBOUR sticker (and left a fragment on its own), and a
    # caption-only idea came out as a bare speech bubble with no character.
    prompt = build_sheet_prompt(_style(), ["«Как там Ванька?»"], age_clause="")
    # 1) idea → tile mapping is explicit and ordered;
    assert "in its OWN tile, in list order" in prompt
    # 2) lettering may not cross into (or repeat in) another tile;
    assert "never spilling into or repeated in another tile" in prompt
    # 3) caption-only ideas still contain the character — no text-only tiles.
    assert "never a tile of just text or a bare speech bubble" in prompt
    assert "the character appears in EVERY sticker" in prompt


def test_sheet_prompt_keeps_custom_descriptions_unquoted() -> None:
    # A free-form custom idea is passed through as a description (no added quotes),
    # while an explicit caption the user quoted keeps its quotes.
    prompt = build_sheet_prompt(_style(), ["дружит с компьютерами", "«Огонь!»"], age_clause="")
    assert "1. дружит с компьютерами" in prompt
    assert "«Огонь!»" in prompt
    # A bare custom description is not force-wrapped in straight quotes.
    assert '"дружит с компьютерами"' not in prompt


def test_sheet_prompt_bans_decor_and_keeps_unused_tiles_empty() -> None:
    # The background stays empty (one absolute clause instead of a noun list);
    # 13 items on a 5×3 grid leave 2 spare tiles that must stay pure magenta.
    thirteen = [f"идея {i}" for i in range(13)]
    prompt = build_sheet_prompt(_style(), thirteen, age_clause="")
    assert "nothing but the stickers is drawn on the magenta" in prompt
    assert "unused tile" in prompt and "15 tiles" in prompt
    # A full grid (15 of 15) has no spare tiles → no confusing clause.
    fifteen = [f"идея {i}" for i in range(15)]
    assert "unused tile" not in build_sheet_prompt(_style(), fifteen, age_clause="")


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


async def test_generate_sheet_gives_up_after_reformulations() -> None:
    model = _RefuserModel(refuse_times=99)  # always refuses
    with pytest.raises(SheetRefusedError):
        await generate_sheet(model, b"CANON", _style(), ["Привет!"], subject_type="adult")
    # refusal doesn't fail over to other models (flash won't un-flag) → one rung of nudges
    assert len(model.calls) == 3
