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

    Standard-block reactions are explicit captions, so they are quoted (the model
    renders the quoted text verbatim). A custom item is passed through as the user
    wrote it: their own quotes mark an exact caption, otherwise it reads as a free
    description of the sticker idea.
    """
    from sticker_service.services.stickers.sets import STANDARD_BLOCK

    item = item.strip()
    return f'"{item}"' if item in STANDARD_BLOCK else item


def build_sheet_prompt(style: Style, captions: list[str], age_clause: str) -> str:
    """Build the single-call grid prompt: per-tile sticker ideas, chroma bg, suffix.

    The list is a set of sticker *ideas/descriptions*, not literal captions. Text is
    drawn only for an item in quotes (an exact caption) or when it genuinely fits;
    otherwise each item becomes a funny, expressive sticker with no forced text.
    """
    from sticker_service.services.postprocess import grid_for

    items = "\n".join(f"{i}. {_as_list_item(c)}" for i, c in enumerate(captions, 1))
    suffix = style.sticker_style_suffix.replace("{age_clause}", age_clause)
    rows, cols = grid_for(len(captions))
    return (
        f"Draw the SAME character as in the reference image as a sheet of {len(captions)} "
        f"die-cut stickers arranged in a regular, even grid of exactly {rows} rows by {cols} "
        f"columns, with large even gaps; stickers must not touch or overlap. "
        f"The numbered list below gives a sticker IDEA for each tile — these are descriptions, "
        f"NOT captions. For EVERY item, draw ONE funny, lively, expressive sticker of the "
        f"character that brings the idea to life. You are free with poses, gestures, facial "
        f"expressions, props, outfits and small scene elements — dress the character or add "
        f"objects whenever it makes the sticker funnier or more emotional, or when the idea "
        f"calls for it. "
        f'Text rule: an item written in quotes («…» or "…") is an EXACT caption — render that '
        f"text. When an item is ONLY a caption (just the quoted text, with no description), "
        f"don't merely write it: make the character playfully act out or react to what the "
        f"caption says, or at least depict its meaning literally. "
        f"An item WITHOUT quotes is only a description: add a short caption ONLY if it "
        f"genuinely suits the sticker, otherwise draw NO text at all. When you do render a "
        f"caption, write it in Russian (Cyrillic) only, cleanly and without glyph artifacts, "
        f"placed where it reads well and does NOT cover the face or the main action; keep it "
        f"small and tidy, wrapping a long caption onto 2-3 lines. Never make a tile that is "
        f"only text, and every tile MUST clearly show the character. "
        f"Items, in order:\n{items}\n"
        f"Each sticker is a die-cut cut-out of the character (with any props, outfit and its "
        f"caption) on a solid flat {CHROMA} magenta background. Keep all props and scene "
        f"objects right next to or touching the character so each sticker stays one connected "
        f"piece; everything that is not part of a sticker — the whole background between, "
        f"around and behind the figures — MUST be solid {CHROMA} magenta, with no shadows, "
        f"gradients, frames or stray splashes. Keep the face, hair and eye color identical to "
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
