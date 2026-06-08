"""Postprocessing: chroma-key, slice a sheet, fit to 512, encode."""

from __future__ import annotations

from sticker_service.services.postprocess.bundle import bundle_zip
from sticker_service.services.postprocess.cover import make_cover
from sticker_service.services.postprocess.preview import compose_preview
from sticker_service.services.postprocess.slice_stickers import (
    CHROMA_DEFAULT,
    chroma_key,
    chroma_key_auto,
    drop_text_strips,
    encode_sticker,
    fit_to_512,
    grid_for,
    grid_slice,
    process_sheet,
    slice_sheet,
)
from sticker_service.services.postprocess.watermark import DEFAULT_TEXT, apply_watermark
from sticker_service.services.postprocess.whatsapp import to_whatsapp, to_whatsapp_pack

__all__ = [
    "CHROMA_DEFAULT",
    "DEFAULT_TEXT",
    "apply_watermark",
    "bundle_zip",
    "chroma_key",
    "chroma_key_auto",
    "compose_preview",
    "drop_text_strips",
    "encode_sticker",
    "fit_to_512",
    "grid_for",
    "grid_slice",
    "make_cover",
    "process_sheet",
    "slice_sheet",
    "to_whatsapp",
    "to_whatsapp_pack",
]
