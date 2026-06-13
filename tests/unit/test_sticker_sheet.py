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


# --- numbered-list caption input (owner's rule, 13.06) ------------------------


def test_parse_caption_items_one_per_line() -> None:
    from sticker_service.services.stickers import parse_caption_items

    text = "1. Привет!\n2) Огонь 🔥\n3. А это ты у него спроси!"
    assert parse_caption_items(text) == ["Привет!", "Огонь 🔥", "А это ты у него спроси!"]


def test_parse_caption_items_inline() -> None:
    from sticker_service.services.stickers import parse_caption_items

    assert parse_caption_items("1. Привет 2. Пока 3. Кидай кубы!") == [
        "Привет",
        "Пока",
        "Кидай кубы!",
    ]
    assert parse_caption_items("1) Раз 2) Два") == ["Раз", "Два"]


def test_parse_caption_items_ignores_preamble_lines() -> None:
    from sticker_service.services.stickers import parse_caption_items

    text = "вот идеи:\n1. Доброе утро\n2. Спокойной ночи"
    assert parse_caption_items(text) == ["Доброе утро", "Спокойной ночи"]


@pytest.mark.parametrize(
    "single",
    [
        "Гол 1:0!",  # digits inside a caption are not a list
        "С 1 сентября",
        "1. Единственный пункт",  # one marker is not a list
        "Счёт 2. Потом 5. Потом 9.",  # numbers not sequential from 1 → one caption
        "обычная подпись",
    ],
)
def test_parse_caption_items_keeps_single_captions_whole(single: str) -> None:
    from sticker_service.services.stickers import parse_caption_items

    assert parse_caption_items(single) == [single]


def test_parse_caption_items_empty() -> None:
    from sticker_service.services.stickers import parse_caption_items

    assert parse_caption_items("") == []
    assert parse_caption_items("   ") == []


def test_build_caption_set_with_personal_and_limit() -> None:
    captions = build_caption_set(personal=["Не в садик!", "Какао!", "Хм...", "Класс!"], limit=15)
    assert len(captions) == 15
    assert "Не в садик!" in captions


# --- prompt ------------------------------------------------------------------


def test_sheet_prompt_turns_standard_reactions_into_drawn_ideas() -> None:
    captions = ["Привет!", "Задумался"]
    prompt = build_sheet_prompt(_style(), captions, age_clause="")
    assert CHROMA in prompt
    assert "«Привет!»" in prompt  # реплика — в кавычках (пишется)
    assert "Задумался" in prompt and "«Задумался»" not in prompt  # эмоция — без кавычек
    assert "watercolor style" in prompt
    assert "{age_clause}" not in prompt  # placeholder resolved
    assert "белой обводкой" in prompt


def test_sheet_prompt_stays_lean() -> None:
    # The brief must not creep back into a micromanaging wall of text: the
    # scaffold around the ideas list stays under a hard budget. Re-armed to
    # 1600 on 2026-06-13 after the owner found the bloated prompt lowered
    # quality — the «Правила надписей» block was trimmed back to compact form.
    prompt = build_sheet_prompt(_style(), ["Привет!"], age_clause="")
    assert len(prompt) < 1600


def test_sheet_prompt_keeps_quote_marks_undrawn_compactly() -> None:
    # The write rule still tells the model NOT to draw the quote marks (live
    # «Серг» defect), but the verbose «брак»/outline-merge clauses were dropped
    # on 2026-06-13 — the prompt is back to the freedom-first brief.
    prompt = build_sheet_prompt(_style(), ['"Спасибо!"'], age_clause="")
    assert "без самих кавычек" in prompt
    # The fiddly caption-plate outline mechanics are gone (they lowered quality).
    assert "обводкой" in prompt  # only the die-cut «белой обводкой» remains
    assert "касается рисунка" not in prompt


def test_sheet_prompt_carries_owner_caption_rules() -> None:
    # Owner-approved contract (2026-06-12) after live caption defects:
    # exactly-once, own tile only, nothing on unquoted tiles, top placement,
    # exact tile count, no bleed onto neighbours.
    prompt = build_sheet_prompt(_style(), ["«Привет!»", "Грустно"], age_clause="")
    assert "Правила надписей" in prompt
    assert "ровно один раз" in prompt
    assert "не пиши ничего" in prompt
    assert "верхней половине" in prompt
    assert "стикеров ровно 2" in prompt
    assert "не заходит на соседний" in prompt


def test_sheet_prompt_pins_ideas_to_tiles_and_text_to_its_tile() -> None:
    # Regression for the «Я проснулся. Технически.» sheet: one idea's caption
    # bled under a NEIGHBOUR sticker (and left a fragment on its own), and a
    # caption-only idea came out as a bare speech bubble with no character.
    prompt = build_sheet_prompt(_style(), ["«Как там Ванька?»"], age_clause="")
    # 1) idea → tile mapping is explicit and ordered;
    assert "один стикер, строго по порядку" in prompt
    # 2) the owner's rule: unquoted = draw, quoted = write without the marks.
    assert "НАРИСОВАТЬ" in prompt
    assert "НАПИСАТЬ" in prompt and "без самих кавычек" in prompt


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
    assert "оставь пустыми" in prompt  # 2 свободных тайла на сетке 5×3
    # A full grid (15 of 15) has no spare tiles → no confusing clause.
    fifteen = [f"идея {i}" for i in range(15)]
    assert "оставь пустыми" not in build_sheet_prompt(_style(), fifteen, age_clause="")


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


def test_prompt_carries_draw_vs_write_rule() -> None:
    # Owner's rule verbatim: unquoted = DRAW, quoted = WRITE without the marks,
    # placed appropriately and not covering the art.
    from sticker_service.config import get_settings
    from sticker_service.services.canonical import StyleLoader

    style = StyleLoader(get_settings().styles_dir).get("watercolor")
    assert style is not None
    prompt = build_sheet_prompt(style, ["Привет!", "Задумался"], "")
    assert "не перекрывая рисунок" in prompt
    assert "«Привет!»" in prompt  # реплика — подпись в кавычках
    assert "«Задумался»" not in prompt  # эмоция — без подписи


def test_canonical_fallbacks_capped_below_4k() -> None:
    # A 4K fallback canonical balloons every later step's memory and OOMed the
    # 1 GB VDS — reference images stay at 2K or below.
    from sticker_service.services.models.gemini import CANONICAL_LADDER

    assert all(size != "4K" for _, size in CANONICAL_LADDER)
