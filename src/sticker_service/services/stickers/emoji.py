"""Assign an emoji to each sticker via a Vision pass, with a safe fallback (§9).

The model looks at a finished sticker and proposes an emoji for its
emotion/gesture. If it returns nothing usable — or errors — we fall back to the
default 🙂 so ``createNewStickerSet`` always has a valid emoji per sticker.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sticker_service.services.models.base import ImageModel, ModelError

logger = logging.getLogger(__name__)

DEFAULT_EMOJI = "🙂"

# Variation selectors / ZWJ that legitimately appear inside a single emoji.
_JOINERS = frozenset({0xFE0F, 0xFE0E, 0x200D, 0x1F3FB, 0x1F3FC, 0x1F3FD, 0x1F3FE, 0x1F3FF})


def _is_emoji_codepoint(char: str) -> bool:
    code = ord(char)
    return (
        0x1F300 <= code <= 0x1FAFF  # symbols & pictographs, emoji, faces
        or 0x2600 <= code <= 0x27BF  # misc symbols + dingbats
        or 0x1F1E6 <= code <= 0x1F1FF  # regional indicators
        or code in (0x2764, 0x2B50, 0x2B55)  # heart, star, circle
    )


def is_single_emoji(value: str) -> bool:
    """True if ``value`` is exactly one emoji (allowing joiners/skin tones)."""
    value = value.strip()
    if not value or value.isascii():
        return False
    cores = [c for c in value if ord(c) not in _JOINERS]
    if not cores or len(cores) > 3:  # a few cores for ZWJ sequences, not a sentence
        return False
    return all(_is_emoji_codepoint(c) for c in cores)


async def assign_emoji(model: ImageModel, image: bytes) -> str:
    """Pick an emoji for one sticker; fall back to 🙂 on invalid output/errors."""
    try:
        raw = await model.pick_emoji(image)
    except ModelError:
        logger.warning("emoji vision call failed; using default")
        return DEFAULT_EMOJI
    candidate = raw.strip()
    if is_single_emoji(candidate):
        return candidate
    logger.info("vision returned non-emoji %r; using default", candidate)
    return DEFAULT_EMOJI


async def assign_emojis(model: ImageModel, images: Sequence[bytes]) -> list[str]:
    """Assign an emoji to each sticker image."""
    return [await assign_emoji(model, image) for image in images]
