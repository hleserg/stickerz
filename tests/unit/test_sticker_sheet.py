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
    captions = ["Привет!", "Задумался"]
    prompt = build_sheet_prompt(_style(), captions, age_clause="")
    assert CHROMA in prompt
    # Every reaction becomes a pure scene description — the model draws no
    # text at all; captions are overlaid in post (deterministic lettering).
    assert "машет рукой в знак приветствия" in prompt
    assert "Подпись" not in prompt  # no lettering orders in items
    assert "watercolor style" in prompt
    assert "{age_clause}" not in prompt  # placeholder resolved
    assert "ABSOLUTELY NO text" in prompt
    assert "yours to invent" in prompt
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
    # 2) the model draws NO text at all (captions are overlaid in post);
    assert "ABSOLUTELY NO text" in prompt
    assert "post-production" in prompt
    # 3) quoted ideas are acted out and the character is in every sticker.
    assert "act it out" in prompt
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


def test_prompt_bans_all_model_lettering() -> None:
    # Prompt-level lettering rules failed under fallback models (live alpha:
    # parenthesized stage directions on a whole pack) — the model now draws no
    # text at all; captions are overlaid deterministically in post-processing.
    from sticker_service.config import get_settings
    from sticker_service.services.canonical import StyleLoader

    style = StyleLoader(get_settings().styles_dir).get("watercolor")
    assert style is not None
    prompt = build_sheet_prompt(style, ["«Привет!»"], "")
    assert "ABSOLUTELY NO text" in prompt
    assert "any drawn text is a defect" in prompt
    assert "Подпись" not in prompt  # no lettering orders sneak into items


def test_canonical_fallbacks_capped_below_4k() -> None:
    # A 4K fallback canonical balloons every later step's memory and OOMed the
    # 1 GB VDS — reference images stay at 2K or below.
    from sticker_service.services.models.gemini import CANONICAL_LADDER

    assert all(size != "4K" for _, size in CANONICAL_LADDER)


def test_caption_overlay_targets_replicas_and_quotes_only() -> None:
    # Owner's rule, now deterministic: text only where the item is a spoken
    # line or the user explicitly quoted a caption; emotions stay clean.
    from sticker_service.services.postprocess.caption import caption_text_for

    assert caption_text_for("Привет!") == "Привет!"  # replica
    assert caption_text_for("Окей 😉") == "Окей"  # emoji stripped
    assert caption_text_for("Люблю") is None  # emotion, NOT a phrase (owner)
    assert caption_text_for("Задумался") is None  # emotion
    assert caption_text_for("Грустно") is None  # emotion
    assert caption_text_for('Тревожное лицо, подпись "Творожность!"') == "Творожность!"
    assert caption_text_for("Смотрит в меню. Подпись: «Стратегический выбор.»") == (
        "Стратегический выбор."
    )
    assert caption_text_for("Укротительница кошек") is None  # plain description


def test_draw_caption_changes_pixels_and_stays_png() -> None:
    from io import BytesIO

    from PIL import Image

    from sticker_service.services.postprocess.caption import draw_caption

    buf = BytesIO()
    Image.new("RGBA", (512, 512), (0, 0, 0, 0)).save(buf, format="PNG")
    blank = buf.getvalue()
    out = draw_caption(blank, "Привет!")
    assert out != blank and out.startswith(b"\x89PNG")
    img = Image.open(BytesIO(out))
    assert img.size == (512, 512)
    # Long captions wrap instead of overflowing the edge.
    out2 = draw_caption(blank, "Я люблю порядок и убираю всё одновременно")
    assert out2 != blank
