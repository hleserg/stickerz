"""Chroma-key a generated sheet, slice it, and fit each sticker to 512 (§7).

The sheet comes back on a solid magenta ``#FF00FF`` background (§6): everything
that color is background, everything else is a sticker — so the white outline no
longer fuses with the background the way white-detection suffered from.

Flow (a sheet is NEVER published as-is — slicing is mandatory, §B.4):
1. chroma-key the background to transparency (pink-family flood + light despill);
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


# PLAYBOOK-START
# pattern: color-family-flood-with-barrier
# status: draft
# problem: removing a decorated background by color *distance* fails when the
#   decoration shares the background's hue family but not its lightness — a pale
#   wash sits euclidean-closer to white than to the key color, so any tolerance
#   wide enough to take it also eats the subject's white outline.
# solution: seed with a strict tolerance, then flood-fill every *connected*
#   pixel that passes a hue-family test instead of a distance ball (for magenta:
#   min(R,B)-G ≥ margin — ≈0 for white/grey/skin/red/blue at any lightness).
#   Keep the subject's silhouette in a non-family color (the white die-cut
#   outline) so the flood has a hard barrier; family-colored details inside the
#   subject survive because they never connect to the seed.
def chroma_key(
    image: Image.Image,
    *,
    chroma: str = CHROMA_DEFAULT,
    tolerance: float = 80.0,
    despill: bool = True,
    loose_tolerance: float = 130.0,
) -> Image.Image:
    """Return an RGBA copy with the chroma background made transparent.

    Pixels within ``tolerance`` (euclidean RGB distance) of ``chroma`` seed the
    background; a hysteresis pass then also removes looser background-ish pixels
    *connected* to that seed, while isolated details on the character are kept.

    For a magenta-family ``chroma`` (the sheet default) the loose mask is the
    whole **pink family** — every pixel whose R and B both clearly dominate G.
    A watercolor wash stays pink at any lightness, but euclidean distance puts a
    light pink further from ``#FF00FF`` than from white, so no distance
    tolerance can absorb the wash without also eating the white die-cut
    outline. The family test scores white/grey/skin/red/blue ≈ 0, so the
    outline is a wall the flood cannot cross: pink clothing or lips inside it
    survive, and only pink connected to the background sea is removed. For any
    other ``chroma`` (the auto-detected fallback) the loose mask is the wider
    ``loose_tolerance`` ball, as before. With ``despill`` the leftover colored
    fringe on edges is pulled toward neutral so stickers don't keep a magenta
    rim.
    """
    cr, cg, cb = _hex_to_rgb(chroma)
    arr = np.asarray(image.convert("RGBA"), dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]

    distance = np.sqrt((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2)
    background = distance < tolerance
    if min(cr, cb) - cg >= 128:
        # Margin 16: pale pinks down to min(R,B)-G==16 flood away, while a white
        # outline blocks once its magenta blend fades past ~94% white.
        loose = background | (np.minimum(r, b) - g >= 16.0)
    else:
        loose = distance < max(loose_tolerance, tolerance)
    structure = ndimage.generate_binary_structure(2, 2)
    labeled = cast(Any, ndimage.label(loose, structure=structure))[0]
    seeds = np.unique(labeled[background])
    seeds = seeds[seeds != 0]  # 0 is the non-loose region, never a seed
    background = np.isin(labeled, seeds)
    arr[..., 3] = np.where(background, 0.0, 255.0)

    if despill:
        # Magenta = high R & B, low G. On kept pixels, trim the R/B excess.
        foreground = ~background
        spill = np.clip((r + b) / 2.0 - g, 0.0, 255.0) * 0.5
        arr[..., 0] = np.where(foreground, np.clip(r - spill, 0, 255), arr[..., 0])
        arr[..., 2] = np.where(foreground, np.clip(b - spill, 0, 255), arr[..., 2])

    return Image.fromarray(arr.astype(np.uint8), mode="RGBA")


# PLAYBOOK-END


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


def _dominant_border_color(arr: np.ndarray, band_frac: float = 0.02) -> tuple[int, int, int]:
    """The most common color along the sheet border (histogram peak, not mean).

    A mean/median washes a gradient into a color nobody painted; the histogram
    peak is the paint the model actually used for most of the frame. Colors are
    quantized to 16-step bins to absorb compression noise.
    """
    h, w = arr.shape[:2]
    band = max(2, int(min(h, w) * band_frac))
    edges = np.concatenate(
        [
            arr[:band, :, :3].reshape(-1, 3),
            arr[-band:, :, :3].reshape(-1, 3),
            arr[:, :band, :3].reshape(-1, 3),
            arr[:, -band:, :3].reshape(-1, 3),
        ]
    ).astype(np.int64)
    q = edges // 16
    keys = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
    top = int(np.bincount(keys).argmax())
    members = edges[keys == top]
    med = np.median(members, axis=0)
    return int(med[0]), int(med[1]), int(med[2])


def outer_flood_key(
    image: Image.Image,
    color: tuple[int, int, int],
    *,
    tolerance: float = 60.0,
    close_px: int = 3,
) -> Image.Image:
    """Key out ``color`` flooding ONLY from the sheet border (owner's design).

    The flood cannot enter a figure: interior pixels of the same color are
    unreachable from the frame. Hairline gaps in the outline are sealed with a
    morphological closing of the figure mask, and any background-ish region NOT
    connected to the border stays opaque (the "roll the flood back" rule).
    This survives the worst case — a background matching the white die-cut
    outline — eating the outline ring but never the figure itself.
    """
    arr = np.asarray(image.convert("RGBA"), dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    cr, cg, cb = color
    candidate = np.sqrt((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2) < tolerance
    figure = ~candidate
    if close_px:
        seal = np.ones((close_px, close_px), bool)
        figure = cast(Any, ndimage.binary_closing(figure, structure=seal))
    structure = ndimage.generate_binary_structure(2, 2)
    labeled = cast(Any, ndimage.label(~figure, structure=structure))[0]
    border = np.unique(
        np.concatenate([labeled[0, :], labeled[-1, :], labeled[:, 0], labeled[:, -1]])
    )
    border = border[border != 0]
    background = np.isin(labeled, border)
    out = arr.copy()
    out[..., 3] = np.where(background, 0.0, out[..., 3])
    return Image.fromarray(out.astype(np.uint8), mode="RGBA")


def _border_clear_frac(rgba: Image.Image, band: int = 4) -> float:
    """Share of border pixels made transparent — the keying honesty criterion."""
    a = np.asarray(rgba)[..., 3]
    edge = np.concatenate(
        [a[:band].ravel(), a[-band:].ravel(), a[:, :band].ravel(), a[:, -band:].ravel()]
    )
    return float((edge == 0).mean()) if edge.size else 0.0


def _is_whitish(color: tuple[int, int, int]) -> bool:
    return min(color) >= 200 and (max(color) - min(color)) <= 40


def _figure_mask(arr: np.ndarray) -> np.ndarray:
    """Opaque AND not white-ish — the colored content of a piece."""
    r, g, b, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    mn = np.minimum(np.minimum(r, g), b)
    mx = np.maximum(np.maximum(r, g), b)
    return (a > 0) & ~((mn >= 200) & (mx - mn <= 40))


def add_outline(piece: Image.Image, width: int) -> Image.Image:
    """Draw a fresh white die-cut ring around a piece's opaque silhouette."""
    src = np.asarray(piece.convert("RGBA"))
    alpha = src[..., 3] > 0
    if width <= 0 or not alpha.any():
        return piece
    pad = width + 2
    h, w = src.shape[:2]
    canvas = np.zeros((h + 2 * pad, w + 2 * pad, 4), dtype=np.uint8)
    canvas[pad : pad + h, pad : pad + w] = src
    padded = canvas[..., 3] > 0
    ring = cast(Any, ndimage.binary_dilation(padded, iterations=width)) & ~padded
    canvas[ring] = (255, 255, 255, 255)
    return _trim_transparent(Image.fromarray(canvas, mode="RGBA"))


def estimate_outline_width(pieces: list[Image.Image]) -> int:
    """Median white-margin width of the pack's healthy stickers."""
    widths: list[float] = []
    for piece in pieces[:6]:
        arr = np.asarray(piece.convert("RGBA"))
        alpha = arr[..., 3] > 0
        fig = _figure_mask(arr)
        if not fig.any() or not alpha.any():
            continue
        dist = cast(Any, ndimage.distance_transform_edt)(alpha)
        boundary = fig & ~cast(Any, ndimage.binary_erosion(fig))
        if boundary.any():
            widths.append(float(np.median(dist[boundary])))
    if not widths:
        return 8
    return int(max(4, min(16, round(sorted(widths)[len(widths) // 2]))))


def split_merged(
    piece: Image.Image, *, target_area: float, outline_width: int
) -> list[Image.Image] | None:
    """Split one merged-outline blob into its stickers (owner's hybrid design).

    Primary path («по контуру + новая обводка»): the colored cores identify the
    figures exactly; the fused white bridge is DISCARDED and every figure gets a
    fresh even die-cut ring — no seam at all. Detached colored satellites (a
    heart by a hand) follow their nearest core. Fallback («рез по талии») when
    cores are unreadable (white-clad figure): cut across the thinnest section
    of the bridge. Returns ``None`` when neither works — the caller fails the
    generation honestly instead of shipping garbage.
    """
    arr = np.asarray(piece.convert("RGBA")).copy()
    fig = _figure_mask(arr)
    structure = ndimage.generate_binary_structure(2, 2)
    # Close generously first: a face and a shirt of the SAME figure may be
    # separated by white clothing — they must read as one core.
    closed = ndimage.binary_closing(fig, structure=np.ones((9, 9), bool))
    labeled, count = cast(Any, ndimage.label(closed, structure=structure))
    core_labels: list[int] = []
    if count:
        areas = np.bincount(labeled.ravel())
        core_min = 0.12 * target_area
        core_labels = [lbl for lbl in range(1, count + 1) if areas[lbl] >= core_min]
    if len(core_labels) >= 2:
        stack = np.stack(
            [cast(Any, ndimage.distance_transform_edt)(labeled != lbl) for lbl in core_labels]
        )
        owner = np.argmin(stack, axis=0)
        parts: list[Image.Image] = []
        for k in range(len(core_labels)):
            keep = fig & (owner == k)
            if not keep.any():
                continue
            sub = arr.copy()
            sub[..., 3] = np.where(keep, sub[..., 3], 0)
            part = _trim_transparent(Image.fromarray(sub, mode="RGBA"))
            if _opaque_area(part) < 64:
                continue
            parts.append(add_outline(part, outline_width))
        return parts if len(parts) >= 2 else None
    return _waist_cut(piece, outline_width=outline_width)


def _waist_cut(piece: Image.Image, *, outline_width: int) -> list[Image.Image] | None:
    """Cut a blob across the thinnest section between its two halves."""
    arr = np.asarray(piece.convert("RGBA")).copy()
    alpha = arr[..., 3] > 0
    h, w = alpha.shape
    horizontal = w >= h  # cut across the longer dimension
    spans = alpha.sum(axis=0) if horizontal else alpha.sum(axis=1)
    size = w if horizontal else h
    lo, hi = int(size * 0.25), int(size * 0.75)
    if hi <= lo + 2:
        return None
    pos = lo + int(np.argmin(spans[lo:hi]))
    if spans[pos] > 0.5 * spans.max():
        return None  # no real waist — a cut here would saw through a figure
    first, second = arr.copy(), arr.copy()
    if horizontal:
        first[:, pos:, 3] = 0
        second[:, :pos, 3] = 0
    else:
        first[pos:, :, 3] = 0
        second[:pos, :, 3] = 0
    parts: list[Image.Image] = []
    for half in (first, second):
        img = _trim_transparent(Image.fromarray(half, mode="RGBA"))
        if _opaque_area(img) < 64:
            return None
        parts.append(add_outline(img, max(2, outline_width // 2)))
    return parts


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
    # Owner's rule: pieces near the pack's median are KEPT; anomalies are what
    # to hunt — much smaller than the median AND/OR of a clearly different
    # shape (a sticker is chunky; a stray lettering strip is long and narrow).
    aspects = [max(p.size) / max(1, min(p.size)) for p in pieces]
    med_area = max(1, sorted(areas)[len(areas) // 2])
    med_aspect = sorted(aspects)[len(aspects) // 2]

    def _anomaly(i: int) -> float:
        return max(0.0, 1.0 - areas[i] / med_area) + max(0.0, aspects[i] - med_aspect) / 2.0

    smallest_first = sorted(range(len(pieces)), key=_anomaly, reverse=True)
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


@dataclass(frozen=True)
class SheetQuality:
    """Verdict on whether sliced pieces actually look like stickers.

    The slicing pipeline always lands on the expected COUNT by construction
    (grid fallback, smallest-extras dropping) — so a broken sheet (shattered
    figures, leaked chroma) used to ship N watermarked scraps that merely
    counted right. ``score`` orders attempts (higher = healthier).
    """

    ok: bool
    reason: str
    score: float
    path: str = "components"


def sheet_quality(
    pieces: list[Image.Image],
    *,
    sheet_size: tuple[int, int],
    grid: tuple[int, int],
    expected: int,
) -> SheetQuality:
    """Cheap shape sanity: pieces must look like stickers, not scraps.

    Checks (no model calls): the count matches; every piece's footprint is
    comparable to the others (min/median area); each piece occupies a sane
    fraction of its grid tile; nothing is a sliver. Any failure means the
    sheet itself is broken and regenerating beats publishing garbage.
    """
    if len(pieces) != expected:
        return SheetQuality(False, f"count {len(pieces)} != {expected}", 0.0)
    areas = [_opaque_area(p) for p in pieces]
    median = sorted(areas)[len(areas) // 2]
    if median <= 0:
        return SheetQuality(False, "empty pieces", 0.0)
    area_ratio = min(areas) / median
    rows, cols = grid
    tile_area = (sheet_size[0] / cols) * (sheet_size[1] / rows)
    tile_fracs = [(p.size[0] * p.size[1]) / tile_area for p in pieces]
    aspects = [max(p.size) / max(1, min(p.size)) for p in pieces]
    score = area_ratio
    if area_ratio < 0.25:
        return SheetQuality(False, f"scrap piece (min/median area {area_ratio:.2f})", score)
    if min(tile_fracs) < 0.20:
        return SheetQuality(False, f"piece too small for its tile ({min(tile_fracs):.2f})", score)
    if max(tile_fracs) > 1.6:
        return SheetQuality(False, f"piece spans tiles ({max(tile_fracs):.2f})", score)
    if max(aspects) > 4.0:
        return SheetQuality(False, f"sliver piece (aspect {max(aspects):.1f})", score)
    return SheetQuality(True, "ok", score)


def process_sheet(
    sheet: bytes | Image.Image,
    *,
    chroma: str = CHROMA_DEFAULT,
    tolerance: float = 80.0,
    min_area: int = 256,
    grid: tuple[int, int] | None = None,
    expected: int | None = None,
) -> list[bytes]:
    """Full pipeline: chroma-key → slice → drop junk → fit 512 → encode."""
    return process_sheet_checked(
        sheet, chroma=chroma, tolerance=tolerance, min_area=min_area, grid=grid, expected=expected
    )[0]


def slice_uploaded_sheet(
    sheet: bytes, *, min_area: int = 256, max_pieces: int = 120
) -> list[bytes]:
    """Slice a USER-UPLOADED sheet into stickers; ``[]`` when honesty fails.

    Unlike the generation pipeline there is no chroma/grid contract: the
    picture may sit on any solid background or already carry transparency.
    Mirrors the auto-key rung of :func:`process_sheet_checked` — dominant
    border color, outer-only flood, ≥80% of the border must clear (a busy
    or gradient background returns ``[]`` instead of blind cuts), a whitish
    background gets the die-cut ring back, stray fragments are dropped.
    A crafted picture that explodes into more than ``max_pieces`` components
    is refused outright — every piece later costs a paid vision call.
    """
    image = Image.open(BytesIO(sheet))
    if max(image.size) > 4096:  # uploads never need 4K+; bound RAM on the VDS
        image.thumbnail((4096, 4096))
    arr = np.asarray(image.convert("RGBA"))
    border_alpha = np.concatenate([arr[0, :, 3], arr[-1, :, 3], arr[:, 0, 3], arr[:, -1, 3]])
    need_ring = False
    if (border_alpha < 16).mean() >= 0.8:  # transparent margins: nothing to key
        keyed = image.convert("RGBA")
    else:
        dominant = _dominant_border_color(arr)
        keyed = outer_flood_key(image, dominant)
        if _border_clear_frac(keyed) < 0.8:
            return []
        need_ring = _is_whitish(dominant)
    pieces = drop_text_strips(slice_sheet(keyed, min_area=min_area))
    if len(pieces) > max_pieces:
        return []
    if need_ring and pieces:
        width = estimate_outline_width(pieces)
        pieces = [add_outline(piece, width) for piece in pieces]
    pieces = drop_outlier_fragments(pieces, expected=None)
    return [encode_sticker(fit_to_512(piece)) for piece in pieces]


def process_sheet_checked(
    sheet: bytes | Image.Image,
    *,
    chroma: str = CHROMA_DEFAULT,
    tolerance: float = 80.0,
    min_area: int = 256,
    grid: tuple[int, int] | None = None,
    expected: int | None = None,
) -> tuple[list[bytes], SheetQuality]:
    """Full pipeline + a :class:`SheetQuality` verdict (owner-designed chain).

    1. Magenta family flood → components. Count matches → done.
    2. The border isn't magenta → key the DOMINANT border color with an
       outer-only flood (cannot enter figures; gaps sealed; non-border leaks
       rolled back). Honesty criterion: ≥80% of the border must clear, else
       the background is a gradient/pattern → give up (no blind cutting).
       A whitish background ate the outline → every piece gets a fresh
       synthetic die-cut ring.
    3. An oversized piece (≥ ~1.7× the per-sticker share) is a merged blob →
       :func:`split_merged` (cores → re-ring; else waist cut).
    4. Shape/area anomalies are dropped; :func:`sheet_quality` issues the final
       verdict. ``quality.ok == False`` means the caller must NOT ship.
    """
    image = sheet if isinstance(sheet, Image.Image) else Image.open(BytesIO(sheet))
    keyed = chroma_key(image, chroma=chroma, tolerance=tolerance)
    pieces = drop_text_strips(slice_sheet(keyed, min_area=min_area))
    path = "components"

    if grid is None or expected is None:
        pieces = drop_outlier_fragments(pieces, expected=expected)
        return (
            [encode_sticker(fit_to_512(piece)) for piece in pieces],
            SheetQuality(True, "unchecked", 1.0, path),
        )

    arr = np.asarray(image.convert("RGBA"))
    if len(pieces) != expected:
        # Maybe the model ignored the magenta contract: key the actual border
        # color instead (outer flood, owner's design).
        dominant = _dominant_border_color(arr)
        cr, cg, cb = _hex_to_rgb(chroma)
        off_contract = (abs(dominant[0] - cr) + abs(dominant[1] - cg) + abs(dominant[2] - cb)) > 120
        if off_contract:
            keyed2 = outer_flood_key(image, dominant)
            if _border_clear_frac(keyed2) >= 0.8:
                pieces2 = drop_text_strips(slice_sheet(keyed2, min_area=min_area))
                if abs(len(pieces2) - expected) < abs(len(pieces) - expected):
                    pieces, path = pieces2, "auto_key"
                    if _is_whitish(dominant):
                        width = estimate_outline_width(pieces)
                        pieces = [add_outline(piece, width) for piece in pieces]

    if 0 < len(pieces) < expected:
        # Merged stickers: an oversized blob vs the per-sticker share is the
        # outlier to cut carefully (owner's rule), never a blind grid.
        total = sum(_opaque_area(piece) for piece in pieces)
        target = total / expected if expected else 0
        if target > 0:
            width = estimate_outline_width(pieces)
            rebuilt: list[Image.Image] = []
            split_any = False
            for piece in pieces:
                if _opaque_area(piece) >= 1.7 * target:
                    parts = split_merged(piece, target_area=target, outline_width=width)
                    if parts:
                        rebuilt.extend(parts)
                        split_any = True
                        continue
                rebuilt.append(piece)
            if split_any:
                pieces = rebuilt
                path = "split" if path == "components" else f"{path}+split"

    pieces = drop_outlier_fragments(pieces, expected=expected)
    quality = sheet_quality(pieces, sheet_size=image.size, grid=grid, expected=expected)
    quality = SheetQuality(quality.ok, quality.reason, quality.score, path)
    return [encode_sticker(fit_to_512(piece)) for piece in pieces], quality
