"""Sticker-sheet generation (phase 2): canonical → one-call sheet + caption sets."""

from __future__ import annotations

from sticker_service.services.stickers.emoji import (
    DEFAULT_EMOJI,
    assign_emoji,
    assign_emojis,
    is_single_emoji,
)
from sticker_service.services.stickers.generate import (
    CHROMA,
    SheetRefusedError,
    build_sheet_prompt,
    generate_sheet,
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
    "SheetRefusedError",
    "assign_emoji",
    "assign_emojis",
    "build_caption_set",
    "build_sheet_prompt",
    "generate_sheet",
    "is_single_emoji",
    "selected_captions",
]
