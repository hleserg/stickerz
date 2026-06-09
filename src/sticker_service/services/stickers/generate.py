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
    " This is a wholesome, age-appropriate cartoon avatar for a family sticker pack.",
    " Friendly, innocent cartoon illustration only; no realism, just a cute drawn character.",
)


class SheetRefusedError(RuntimeError):
    """The model refused to generate the sheet after all retries (§6)."""


def _as_list_item(item: str) -> str:
    """Render one sheet item for the prompt.

    A standard-block reaction becomes its unquoted scene description — the
    emotion is drawn, never captioned (quoting it would order the model to
    letter the label). A custom item is passed through as the user wrote it:
    their own quotes mark an exact caption, otherwise it reads as a free
    description of the sticker idea.
    """
    from sticker_service.services.stickers.sets import STANDARD_IDEAS

    item = item.strip()
    return STANDARD_IDEAS.get(item, item)


def build_sheet_prompt(style: Style, captions: list[str], age_clause: str) -> str:
    """Build the single-call grid prompt: a short, freedom-first art brief.

    Deliberately lean — the model draws best when briefed, not micromanaged.
    Only the load-bearing constraints stay (chroma background, grid, die-cut
    outline, connectivity, identity); everything creative is handed to the
    model. Text appears ONLY where an idea asks for it in quotes; emotion is
    shown in the drawing, not written under it.
    """
    from sticker_service.services.postprocess import grid_for

    items = "\n".join(f"{i}. {_as_list_item(c)}" for i, c in enumerate(captions, 1))
    suffix = style.sticker_style_suffix.replace("{age_clause}", age_clause)
    rows, cols = grid_for(len(captions))
    # Junk (washes, flourishes) clusters in leftover cells — forbid them explicitly.
    spare = rows * cols - len(captions)
    empty_clause = (
        f" Items fill only {len(captions)} of the {rows * cols} tiles: leave every unused "
        f"tile completely empty — pure flat {CHROMA} magenta, nothing drawn in it."
        if spare
        else ""
    )
    return (
        f"Draw the SAME character as in the reference image as a sheet of {len(captions)} "
        f"stickers in an even grid of exactly {rows} rows by {cols} columns on a solid flat "
        f"{CHROMA} magenta background. Wide clean gaps; stickers never touch; nothing but "
        f"the stickers is drawn on the magenta. Each sticker is a die-cut cut-out with a "
        f"clean white outline; keep props touching the character so each sticker is one "
        f"connected piece. Keep the face, hair and eye color identical to the reference.\n"
        f"Each numbered idea below is ONE sticker in its OWN tile, in list order (left to "
        f"right, top to bottom). Make it funny, warm and full of character — the pose, "
        f"expression, outfit, props and composition are yours to invent. Show the emotion "
        f"in the drawing itself and add NO text unless the idea explicitly asks for it. "
        f'When an idea has text in quotes («…» or "…"), letter exactly that text in clean '
        f"Russian Cyrillic, placed where it sits naturally in the composition — not as a "
        f"banner pinned to the bottom. Lettering belongs only to its own sticker: keep it "
        f"inside that sticker's tile, never spilling into or repeated in another tile. If "
        f"an idea is ONLY a quoted caption, act it out: draw the character living that "
        f"phrase, caption worked in — never a tile of just text or a bare speech bubble; "
        f"the character appears in EVERY sticker.{empty_clause}\n"
        f"Ideas:\n{items}\n"
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
