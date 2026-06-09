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

import math
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


def grid_for(n: int) -> tuple[int, int]:
    """Pick a 3-wide portrait ``(rows, cols)`` grid for ``n`` stickers.

    Three columns matches the phone-friendly sheet the model draws best (e.g. 15
    stickers → 5×3); fewer than three stickers shrink the columns to fit.
    """
    cols = min(3, max(1, n))
    rows = math.ceil(n / cols)
    return rows, cols


def _detect_bg_color(arr: np.ndarray) -> tuple[int, int, int]:
    """Estimate the background color from the four corners of an image array."""
    s = max(2, min(arr.shape[0], arr.shape[1]) // 16)
    corners = np.concatenate(
        [
            arr[:s, :s].reshape(-1, 4),
            arr[:s, -s:].reshape(-1, 4),
            arr[-s:, :s].reshape(-1, 4),
            arr[-s:, -s:].reshape(-1, 4),
        ]
    )
    med = np.median(corners[:, :3], axis=0)
    return int(med[0]), int(med[1]), int(med[2])


def chroma_key_auto(
    image: Image.Image, *, tolerance: float = 70.0, despill: bool = False
) -> Image.Image:
    """Chroma-key using the auto-detected corner background color (any solid bg)."""
    arr = np.asarray(image.convert("RGBA"))
    r, g, b = _detect_bg_color(arr)
    return chroma_key(image, chroma=f"#{r:02x}{g:02x}{b:02x}", tolerance=tolerance, despill=despill)


def _trim_transparent(image: Image.Image) -> Image.Image:
    """Crop an RGBA image to the bounding box of its non-transparent pixels."""
    arr = np.asarray(image.convert("RGBA"))
    ys, xs = np.where(arr[..., 3] > 0)
    if xs.size == 0:
        return image
    return image.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))


def _opaque_area(image: Image.Image) -> int:
    """Count the non-transparent pixels of an RGBA image (its real footprint)."""
    return int((np.asarray(image.convert("RGBA"))[..., 3] > 0).sum())


# PLAYBOOK-START
# pattern: keep-dominant-component-plus-neighbours
# status: draft
# problem: segmenting a subject from a tile leaves stray blobs the colour key
#   missed (corner scenery, a detached glyph, paint splatter) that must go,
#   while small *adjacent* blobs that belong to the subject (a heart by the
#   hand, an emoji on the caption) must stay.
# solution: keep the largest component; also keep any component that is either
#   large relative to it OR whose bounding box is near the main one's (grown by
#   a margin); zero the rest. Proximity — not mere size — is the keep signal.
def _clean_satellites(
    image: Image.Image, *, margin_frac: float = 0.12, keep_frac: float = 0.30
) -> Image.Image:
    """Keep a tile's main figure (+ blobs touching it) and drop far stray bits.

    A painterly tile can carry scenery the chroma key missed — a picture frame in
    a corner, a stray eye, a paint splash — sitting *away* from the central
    figure. Keep the largest component plus any component that is large
    (``keep_frac`` of it) or near it (its box overlaps the figure box grown by
    ``margin_frac`` of the tile), zeroing everything else. A heart or 😉 next to
    the face survives; a corner shard does not.
    """
    arr = np.asarray(image.convert("RGBA")).copy()
    solid = arr[..., 3] > 0
    if not solid.any():
        return image
    structure = ndimage.generate_binary_structure(2, 2)
    labeled, count = cast(Any, ndimage.label(solid, structure=structure))
    if count <= 1:
        return _trim_transparent(image)
    slices = ndimage.find_objects(labeled)
    # One pass over the label image yields every component's area; index 0 is the
    # background, so labels line up with ``areas[label]`` (no 1-based bookkeeping).
    areas = np.bincount(labeled.ravel(), minlength=count + 1)
    main = int(np.argmax(areas[1:])) + 1
    main_area = int(areas[main])
    mr, mc = slices[main - 1]
    height, width = arr.shape[:2]
    margin_y, margin_x = margin_frac * height, margin_frac * width
    kept = [main]
    for idx, sl in enumerate(slices, start=1):
        if idx == main or sl is None:
            continue
        if int(areas[idx]) >= keep_frac * main_area:
            kept.append(idx)
            continue
        rr, cc = sl
        near = (
            rr.start <= mr.stop + margin_y
            and rr.stop >= mr.start - margin_y
            and cc.start <= mc.stop + margin_x
            and cc.stop >= mc.start - margin_x
        )
        if near:
            kept.append(idx)
    # Build the keep-mask once instead of OR-ing a full-image mask per component.
    arr[..., 3] = np.where(np.isin(labeled, kept), arr[..., 3], 0)
    return _trim_transparent(Image.fromarray(arr, mode="RGBA"))


# PLAYBOOK-END


def grid_slice(
    image: Image.Image, rows: int, cols: int, *, tolerance: float = 70.0, min_content: int = 64
) -> list[Image.Image]:
    """Cut a sheet into a regular ``rows×cols`` grid; per-cell auto background removal.

    Fallback for when the model didn't honor the exact chroma background or left
    no gaps between stickers, so connected-component slicing can't separate them.
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    cell_w, cell_h = width / cols, height / rows
    out: list[Image.Image] = []
    for r in range(rows):
        for c in range(cols):
            box = (
                round(c * cell_w),
                round(r * cell_h),
                round((c + 1) * cell_w),
                round((r + 1) * cell_h),
            )
            cell = _clean_satellites(chroma_key_auto(rgba.crop(box), tolerance=tolerance))
            if _opaque_area(cell) >= min_content:
                out.append(cell)
    return out


def drop_text_strips(pieces: list[Image.Image]) -> list[Image.Image]:
    """Remove detached caption-only fragments so every sticker keeps the character.

    When the model renders a caption in the background gap (not on the figure),
    connected-component slicing cuts that line of text into its own piece — a
    text-only "sticker", which is never valid: a sticker must depict the drawn
    character (a caption that sits *on* the character stays part of its taller
    component and is preserved). A detached caption is a short, wide strip, so we
    drop pieces that are much shorter than the typical piece and clearly wide.
    Never returns an empty list.
    """
    if len(pieces) < 2:
        return pieces
    heights = sorted(p.size[1] for p in pieces)
    median_h = heights[len(heights) // 2]
    kept = [p for p in pieces if not (p.size[1] < 0.5 * median_h and p.size[0] > 1.6 * p.size[1])]
    return kept or pieces


def drop_outlier_fragments(
    pieces: list[Image.Image], *, expected: int | None = None, area_frac: float = 0.4
) -> list[Image.Image]:
    """Drop small-area outlier pieces: stray glyphs, scenery shards, paint splashes.

    A valid sticker fills its tile with the character, so every real piece has a
    similar opaque footprint; a detached letter (e.g. a lone «Я»), a corner
    picture-frame or a splash is an outlier. Two modes:

    - ``expected`` known: cap to it by dropping the smallest extras. A stray
      fragment isn't always *small* (a duplicated limb, a blob the chroma key
      missed), so an area threshold alone can't catch it — we must land on
      exactly ``expected`` (this is the "7 pieces for 6 captions" fix).
    - ``expected`` unknown: drop only clear small-area outliers (below
      ``area_frac`` of the median).

    Complements ``drop_text_strips`` (short-wide caption lines). Never returns an
    empty list.
    """
    if len(pieces) < 2:
        return pieces
    if expected is not None and len(pieces) <= expected:
        return pieces  # already at/under target — nothing to drop, skip the scan
    areas = [_opaque_area(p) for p in pieces]
    smallest_first = sorted(range(len(pieces)), key=lambda i: areas[i])
    if expected is not None:
        to_drop = set(smallest_first[: len(pieces) - expected])
    else:
        median = sorted(areas)[len(areas) // 2]
        if median == 0:
            return pieces
        threshold = area_frac * median
        to_drop = {i for i in smallest_first if areas[i] < threshold}
    kept = [p for i, p in enumerate(pieces) if i not in to_drop]
    return kept or pieces


def process_sheet(
    sheet: bytes | Image.Image,
    *,
    chroma: str = CHROMA_DEFAULT,
    tolerance: float = 80.0,
    min_area: int = 256,
    grid: tuple[int, int] | None = None,
    expected: int | None = None,
) -> list[bytes]:
    """Full pipeline: chroma-key → slice → drop junk → fit 512 → encode.

    Detached caption-only fragments are dropped first so no sticker is text without
    a character. Only when connected-component slicing falls short of the expected
    number of stickers do we fall back to cutting the regular ``grid`` (for sheets
    where the model used an off background or left no gaps); we then keep whichever
    result is closest to ``expected``. ``expected`` defaults to ``rows*cols-1`` so
    callers that don't know the exact count keep the previous behaviour. Finally
    small-area outliers (stray glyphs, scenery shards, paint splashes the chroma
    key missed) are dropped down to ``expected`` so no junk tile is published.
    """
    image = sheet if isinstance(sheet, Image.Image) else Image.open(BytesIO(sheet))
    keyed = chroma_key(image, chroma=chroma, tolerance=tolerance)
    pieces = drop_text_strips(slice_sheet(keyed, min_area=min_area))
    if grid is not None:
        rows, cols = grid
        target = expected if expected is not None else rows * cols - 1
        if len(pieces) < target:
            grid_pieces = drop_text_strips(grid_slice(image, rows, cols))
            if expected is not None:
                # Keep whichever slicing landed closest to the real caption count.
                pieces = min((pieces, grid_pieces), key=lambda ps: abs(len(ps) - expected))
            else:
                pieces = grid_pieces
    pieces = drop_outlier_fragments(pieces, expected=expected)
    return [encode_sticker(fit_to_512(piece)) for piece in pieces]
