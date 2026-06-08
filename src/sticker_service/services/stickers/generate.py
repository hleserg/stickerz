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


def build_sheet_prompt(style: Style, captions: list[str], age_clause: str) -> str:
    """Build the single-call grid prompt with captions, chroma bg, and suffix."""
    from sticker_service.services.postprocess import grid_for

    caption_list = "; ".join(f'"{c}"' for c in captions)
    suffix = style.sticker_style_suffix.replace("{age_clause}", age_clause)
    rows, cols = grid_for(len(captions))
    return (
        f"Draw the SAME character as in the reference image as a sheet of {len(captions)} "
        f"stickers arranged in a regular, even grid of exactly {rows} rows by {cols} columns. "
        f"EVERY sticker MUST clearly depict the drawn character in a "
        f"different emotion or pose with a Russian caption, in this order: {caption_list}. "
        f"Never make a tile that is only text — text alone is not a sticker. "
        f"Place each caption directly ON the character (overlapping the lower part of the "
        f"figure), not floating alone in the background gap. "
        f"All captions strictly in Russian (Cyrillic) only — no other language; "
        f"render the text cleanly, with no character artifacts. "
        f"Each tile must contain ONLY the character with its caption — absolutely no "
        f"background scenery, furniture, picture frames, extra faces or body parts, and no "
        f"decorative paint splashes or splatter. Everything that is not the character MUST be "
        f"the solid {CHROMA} magenta. "
        f"The background MUST be a solid flat {CHROMA} magenta everywhere between and around "
        f"the stickers — no shadows, no gradients on the background. Use large even gaps; "
        f"stickers must not touch or overlap. Keep the face, hair and eye color identical to "
        f"the reference. {suffix}"
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
