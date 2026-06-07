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
from sticker_service.services.models.base import ImageModel, ModelRefusalError

logger = logging.getLogger(__name__)

CHROMA = "#FF00FF"
MAX_REFUSAL_RETRIES = 3

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
    caption_list = "; ".join(f'"{c}"' for c in captions)
    suffix = style.sticker_style_suffix.replace("{age_clause}", age_clause)
    return (
        f"Draw the SAME character as in the reference image as a sheet of {len(captions)} "
        f"stickers arranged in a regular, even grid. Each sticker shows the character in a "
        f"different emotion or pose with a Russian caption, in this order: {caption_list}. "
        f"Render the Russian (Cyrillic) text cleanly, with no character artifacts. "
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
    max_refusal_retries: int = MAX_REFUSAL_RETRIES,
) -> bytes:
    """Generate the sheet in one call; retry with reformulation on refusal."""
    age_clause = build_age_clause(subject_type, child_age)
    base_prompt = build_sheet_prompt(style, captions, age_clause)

    for attempt in range(max_refusal_retries):
        reformulation = _REFORMULATIONS[min(attempt, len(_REFORMULATIONS) - 1)]
        try:
            return await model.generate(base_prompt + reformulation, refs=[canonical])
        except ModelRefusalError:
            logger.warning(
                "sheet generation refused (attempt %d/%d); reformulating",
                attempt + 1,
                max_refusal_retries,
            )
    raise SheetRefusedError(
        f"model refused the sheet after {max_refusal_retries} attempts (style={style.style_id})"
    )
