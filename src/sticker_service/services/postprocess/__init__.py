"""Postprocessing: chroma-key, slice a sheet, fit to 512, encode."""

from __future__ import annotations

from sticker_service.services.postprocess.bundle import bundle_zip
from sticker_service.services.postprocess.preview import compose_preview
from sticker_service.services.postprocess.slice_stickers import (
    CHROMA_DEFAULT,
    chroma_key,
    chroma_key_auto,
    encode_sticker,
    fit_to_512,
    grid_for,
    grid_slice,
    process_sheet,
    slice_sheet,
)
from sticker_service.services.postprocess.whatsapp import to_whatsapp, to_whatsapp_pack

__all__ = [
    "CHROMA_DEFAULT",
    "bundle_zip",
    "chroma_key",
    "chroma_key_auto",
    "compose_preview",
    "encode_sticker",
    "fit_to_512",
    "grid_for",
    "grid_slice",
    "process_sheet",
    "slice_sheet",
    "to_whatsapp",
    "to_whatsapp_pack",
]
