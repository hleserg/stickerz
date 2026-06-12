"""Generate the whole sticker sheet in ONE model call (§6, §B.4).

The entire set (~15) is produced as a single grid image from the canonical
reference, with Russian captions drawn by the model itself and a solid magenta
``#FF00FF`` background for trivial chroma slicing afterward (§7). On a
child-safety refusal we retry up to 3 times with a gentler reformulation (§6).
"""

from __future__ import annotations

import logging

from sticker_service.db.models import SubjectType
from sticker_service.services.canonical.engine import build_age_clause
from sticker_service.services.canonical.schema import Style
from sticker_service.services.models.base import ImageModel, ModelRefusalError, generate_via_ladder
from sticker_service.services.models.gemini import SHEET_LADDER

logger = logging.getLogger(__name__)

CHROMA = "#FF00FF"

# Appended on each refusal retry to steer away from the safety trigger (§6).
_REFORMULATIONS: tuple[str, ...] = (
    "",
    " Это добрый мультяшный персонаж для семейного стикерпака.",
    " Только дружелюбная мультяшная иллюстрация, без реализма.",
)


class SheetRefusedError(RuntimeError):
    """The model refused to generate the sheet after all retries (§6)."""


def prompt_idea(item: str) -> str:
    """Exact prompt line for one idea — and the checklist button text.

    Full transparency (owner's rule): the button shows precisely what the
    model will receive. Standard buttons map through STANDARD_PROMPTS;
    anything else (user customs) passes verbatim.
    """
    from sticker_service.services.stickers.sets import STANDARD_PROMPTS

    item = item.strip()
    return STANDARD_PROMPTS.get(item, item)


def build_sheet_prompt(style: Style, captions: list[str], age_clause: str) -> str:
    """Build the single-call grid prompt: a short, freedom-first art brief.

    Deliberately lean — the model draws best when briefed, not micromanaged.
    Only the load-bearing constraints stay (chroma background, grid, die-cut
    outline, connectivity, identity); everything creative is handed to the
    model. Text appears ONLY where an idea asks for it in quotes; emotion is
    shown in the drawing, not written under it. The «Правила надписей» block
    is the owner-approved contract (2026-06-12): live sheets duplicated,
    dropped and stranded captions across tiles, so exactly-once / own-tile /
    top-placement are spelled out instead of trusted.
    """
    from sticker_service.services.postprocess import grid_for

    items = "\n".join(f"{i}. {prompt_idea(c)}" for i, c in enumerate(captions, 1))
    suffix = style.sticker_style_suffix.replace("{age_clause}", age_clause)
    rows, cols = grid_for(len(captions))
    # Junk (washes, flourishes) clusters in leftover cells — forbid them explicitly.
    spare = rows * cols - len(captions)
    empty_clause = f" Лишние {spare} тайл(а) оставь пустыми (чистый {CHROMA})." if spare else ""
    return (
        f"Нарисуй персонажа с референса листом из {len(captions)} стикеров: сетка "
        f"{rows}×{cols}, фон — сплошной {CHROMA}.\n"
        f"Лицо, причёска и цвет глаз — в точности как на референсе.\n"
        f"Каждый стикер — отдельная наклейка одним куском, с белой обводкой; стикеры "
        f"не соприкасаются; ничего из одного стикера — ни рисунок, ни предмет, ни "
        f"надпись — не заходит на соседний.\n"
        f"Одна идея из списка = один стикер, строго по порядку (слева направо, сверху "
        f"вниз). Идей {len(captions)} — стикеров ровно {len(captions)}.\n"
        f"Всё, что в идее написано без кавычек, — это то, что надо НАРИСОВАТЬ. Всё, "
        f"что в кавычках (кавычки бывают «», \"\" или ''), — это надо НАПИСАТЬ без "
        f"самих кавычек, в уместном месте, не перекрывая рисунок. Если идея состоит "
        f"из одной надписи в кавычках — нарисуй персонажа, обыгрывающего её, и "
        f"подпиши этим текстом: без кавычек и не перекрывая текстом картинку.\n"
        f"Правила надписей: каждая надпись появляется на листе ровно один раз — "
        f"целиком, без изменений, на стикере своей идеи; на одном стикере не бывает "
        f"двух надписей; если в идее нет кавычек — на её стикере не пиши ничего; "
        f"надпись размещай в верхней половине стикера, никогда — у нижнего края "
        f"(низ занят водяным знаком и интерфейсом Telegram).{empty_clause}\n"
        f"Идеи:\n{items}\n"
        f"{suffix}"
    ).strip()


async def generate_sheet(
    model: ImageModel,
    canonical: bytes,
    style: Style,
    captions: list[str],
    *,
    subject_type: SubjectType,
    child_age: int | None = None,
) -> bytes:
    """Generate the sheet in one call, walking the model/resolution ladder (§6, HLE-1055)."""
    age_clause = build_age_clause(subject_type, child_age)
    base_prompt = build_sheet_prompt(style, captions, age_clause)

    logger.info("sheet: generating %d stickers in one call", len(captions))
    try:
        sheet = await generate_via_ladder(
            model, base_prompt, [canonical], SHEET_LADDER, reformulations=_REFORMULATIONS
        )
    except ModelRefusalError as exc:
        raise SheetRefusedError(
            f"model refused the sheet after reformulations (style={style.style_id})"
        ) from exc
    logger.info("sheet: generated (%d bytes)", len(sheet))
    return sheet
