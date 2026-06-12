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


def _is_regional_indicator(char: str) -> bool:
    return 0x1F1E6 <= ord(char) <= 0x1F1FF


def is_single_emoji(value: str) -> bool:
    """True if ``value`` is exactly one emoji (allowing joiners/skin tones)."""
    value = value.strip()
    if not value or value.isascii():
        return False
    cores = [c for c in value if ord(c) not in _JOINERS]
    if not cores or len(cores) > 3:  # a few cores for ZWJ sequences, not a sentence
        return False
    flags = [c for c in cores if _is_regional_indicator(c)]
    # Regional indicators are an emoji only as a PAIR (a flag): a lone half
    # («🇷») passes the range check but Telegram rejects it (live 13.06).
    if flags and (len(flags) != 2 or len(cores) != 2):
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


def extract_emoji(text: str) -> str | None:
    """Return the first emoji already present in ``text`` (with joiners), else None."""
    chars = list(text)
    for i, ch in enumerate(chars):
        if _is_emoji_codepoint(ch):
            cluster = ch
            j = i + 1
            # A flag is a PAIR of regional indicators — keep them together,
            # or «🇷🇺» would be clipped to the half-flag Telegram rejects.
            if _is_regional_indicator(ch) and j < len(chars) and _is_regional_indicator(chars[j]):
                cluster += chars[j]
                j += 1
            while j < len(chars) and ord(chars[j]) in _JOINERS:
                cluster += chars[j]
                j += 1
            return cluster if is_single_emoji(cluster) else None
    return None


def emoji_for_caption(caption: str) -> str | None:
    """A known emoji for ``caption`` without a model call, or None if unknown.

    Tries the standard-caption map first, then any emoji the user typed into a
    custom caption (e.g. "Огонь 🔥"). Returns None for plain custom captions, so
    the caller falls back to the vision model only when it actually helps.
    """
    from sticker_service.services.stickers.sets import STANDARD_EMOJI

    known = STANDARD_EMOJI.get(caption.strip())
    return known if known else extract_emoji(caption)


async def assign_emojis(
    model: ImageModel, images: Sequence[bytes], captions: Sequence[str] | None = None
) -> list[str]:
    """Assign an emoji to each sticker, skipping the vision call when the caption
    already implies one (standard block or an emoji typed by the user)."""
    caps = list(captions) if captions is not None else []
    out: list[str] = []
    for i, image in enumerate(images):
        known = emoji_for_caption(caps[i]) if i < len(caps) else None
        out.append(known if known else await assign_emoji(model, image))
    return out
