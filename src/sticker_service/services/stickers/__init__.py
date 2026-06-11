"""Sticker-sheet generation (phase 2): canonical → one-call sheet + caption sets."""

from __future__ import annotations

from sticker_service.services.stickers.emoji import (
    DEFAULT_EMOJI,
    assign_emoji,
    assign_emojis,
    emoji_for_caption,
    extract_emoji,
    is_single_emoji,
)
from sticker_service.services.stickers.generate import (
    CHROMA,
    SheetRefusedError,
    build_sheet_prompt,
    generate_sheet,
    prompt_idea,
)
from sticker_service.services.stickers.meme_pool import (
    MemeIdea,
    active_pool,
    bundled_pool,
    parse_pool,
    sample_default_mix,
)
from sticker_service.services.stickers.sets import (
    MAX_CAPTIONS,
    PER_PAGE,
    STANDARD_BLOCK,
    build_caption_set,
    selected_captions,
)

__all__ = [
    "CHROMA",
    "DEFAULT_EMOJI",
    "MAX_CAPTIONS",
    "PER_PAGE",
    "STANDARD_BLOCK",
    "MemeIdea",
    "SheetRefusedError",
    "active_pool",
    "assign_emoji",
    "assign_emojis",
    "build_caption_set",
    "build_sheet_prompt",
    "bundled_pool",
    "emoji_for_caption",
    "extract_emoji",
    "generate_sheet",
    "is_single_emoji",
    "parse_pool",
    "prompt_idea",
    "sample_default_mix",
    "selected_captions",
]
