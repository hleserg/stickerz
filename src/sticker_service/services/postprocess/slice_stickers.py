"""Chroma-key a generated sheet, slice it, and fit each sticker to 512 (§7).

The sheet comes back on a solid magenta ``#FF00FF`` background (§6): everything
that color is background, everything else is a sticker — so the white outline no
longer fuses with the background the way white-detection suffered from.

Flow (a sheet is NEVER published as-is — slicing is mandatory, §B.4):
1. chroma-key the background to transparency (+ light despill of the fringe);
2. split into connected components (one per sticker);
3. fit each to 512 on its longest side (no distortion) and encode ≤512 KB.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Any, cast

import numpy as np
from PIL import Image
from scipy import ndimage

CHROMA_DEFAULT = "#FF00FF"
_MAX_BYTES = 512 * 1024
_TARGET_SIDE = 512


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def chroma_key(
    image: Image.Image,
    *,
    chroma: str = CHROMA_DEFAULT,
    tolerance: float = 80.0,
    despill: bool = True,
) -> Image.Image:
    """Return an RGBA copy with the chroma background made transparent.

    Pixels within ``tolerance`` (euclidean RGB distance) of ``chroma`` become
    fully transparent. With ``despill`` the leftover colored fringe on edges is
    pulled toward neutral so stickers don't keep a magenta rim.
    """
    cr, cg, cb = _hex_to_rgb(chroma)
    arr = np.asarray(image.convert("RGBA"), dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    distance = np.sqrt((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2)
    background = distance < tolerance
    arr[..., 3] = np.where(background, 0.0, 255.0)

    if despill:
        # Magenta = high R & B, low G. On kept pixels, trim the R/B excess.
        foreground = ~background
        spill = np.clip((r + b) / 2.0 - g, 0.0, 255.0) * 0.5
        arr[..., 0] = np.where(foreground, np.clip(r - spill, 0, 255), arr[..., 0])
        arr[..., 2] = np.where(foreground, np.clip(b - spill, 0, 255), arr[..., 2])

    return Image.fromarray(arr.astype(np.uint8), mode="RGBA")


@dataclass(frozen=True)
class _Region:
    top: int
    left: int
    image: Image.Image


def slice_sheet(rgba: Image.Image, *, min_area: int = 256) -> list[Image.Image]:
    """Split a transparent-background RGBA sheet into individual stickers.

    Connected components on the alpha channel (8-connectivity); specks below
    ``min_area`` pixels are dropped. Output is ordered top-to-bottom,
    left-to-right, and each crop keeps only its own component's pixels.
    """
    arr = np.asarray(rgba.convert("RGBA"))
    solid = arr[..., 3] > 0
    structure = ndimage.generate_binary_structure(2, 2)
    # scipy stubs over-narrow the return type; cast to unpack (array, count).
    labeled = cast(Any, ndimage.label(solid, structure=structure))[0]
    slices = ndimage.find_objects(labeled)

    regions: list[_Region] = []
    for index, bbox in enumerate(slices, start=1):
        if bbox is None:
            continue
        row_slice, col_slice = bbox
        mask = labeled[row_slice, col_slice] == index
        if int(mask.sum()) < min_area:
            continue
        crop = arr[row_slice, col_slice].copy()
        # Zero out any other component that intrudes into this bounding box.
        crop[..., 3] = np.where(mask, crop[..., 3], 0)
        regions.append(
            _Region(
                top=row_slice.start,
                left=col_slice.start,
                image=Image.fromarray(crop, mode="RGBA"),
            )
        )

    regions.sort(key=lambda r: (r.top, r.left))
    return [r.image for r in regions]


def fit_to_512(image: Image.Image, *, side: int = _TARGET_SIDE) -> Image.Image:
    """Scale so the longest side is exactly ``side`` px, preserving aspect."""
    rgba = image.convert("RGBA")
    w, h = rgba.size
    if w >= h:
        new_w, new_h = side, max(1, round(h * side / w))
    else:
        new_w, new_h = max(1, round(w * side / h)), side
    return rgba.resize((new_w, new_h), Image.Resampling.LANCZOS)


def encode_sticker(image: Image.Image, *, max_bytes: int = _MAX_BYTES) -> bytes:
    """Encode to PNG, falling back to progressively smaller WebP if too big."""
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    data = buffer.getvalue()
    if len(data) <= max_bytes:
        return data
    for quality in (95, 90, 80, 70, 60):  # pragma: no cover - large-image path
        buffer = BytesIO()
        image.save(buffer, format="WEBP", quality=quality, method=6)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
    return data  # pragma: no cover - best effort


def process_sheet(
    sheet: bytes | Image.Image,
    *,
    chroma: str = CHROMA_DEFAULT,
    tolerance: float = 80.0,
    min_area: int = 256,
) -> list[bytes]:
    """Full pipeline: chroma-key → slice → fit 512 → encode. Returns PNG/WebP bytes."""
    image = sheet if isinstance(sheet, Image.Image) else Image.open(BytesIO(sheet))
    keyed = chroma_key(image, chroma=chroma, tolerance=tolerance)
    pieces = slice_sheet(keyed, min_area=min_area)
    return [encode_sticker(fit_to_512(piece)) for piece in pieces]
