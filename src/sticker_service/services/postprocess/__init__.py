"""Postprocessing: chroma-key, slice a sheet, fit to 512, encode."""

from __future__ import annotations

from PIL import Image

from sticker_service.services.postprocess.bundle import bundle_zip
from sticker_service.services.postprocess.cover import make_cover
from sticker_service.services.postprocess.preview import compose_preview
from sticker_service.services.postprocess.slice_stickers import (
    CHROMA_DEFAULT,
    SheetQuality,
    add_outline,
    chroma_key,
    chroma_key_auto,
    drop_outlier_fragments,
    drop_text_strips,
    encode_sticker,
    fit_to_512,
    grid_for,
    outer_flood_key,
    process_sheet,
    process_sheet_checked,
    sheet_quality,
    slice_sheet,
    slice_uploaded_sheet,
    split_merged,
)
from sticker_service.services.postprocess.watermark import DEFAULT_TEXT, apply_watermark
from sticker_service.services.postprocess.whatsapp import to_whatsapp, to_whatsapp_pack

# Decompression-bomb ceiling for every PIL decode in the service. A 4K sheet is
# ~16.8 MP; nothing legitimate comes close to this cap, while a crafted file
# that would balloon past it raises instead of eating the VDS's RAM.
Image.MAX_IMAGE_PIXELS = 64_000_000

__all__ = [
    "CHROMA_DEFAULT",
    "DEFAULT_TEXT",
    "SheetQuality",
    "add_outline",
    "apply_watermark",
    "bundle_zip",
    "chroma_key",
    "chroma_key_auto",
    "compose_preview",
    "drop_outlier_fragments",
    "drop_text_strips",
    "encode_sticker",
    "fit_to_512",
    "grid_for",
    "make_cover",
    "outer_flood_key",
    "process_sheet",
    "process_sheet_checked",
    "sheet_quality",
    "slice_sheet",
    "slice_uploaded_sheet",
    "split_merged",
    "to_whatsapp",
    "to_whatsapp_pack",
]
