"""Tests for chroma-key slicing and 512 fitting (§7, §B.4)."""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw

from sticker_service.services.postprocess import (
    chroma_key,
    chroma_key_auto,
    encode_sticker,
    fit_to_512,
    grid_for,
    grid_slice,
    process_sheet,
    slice_sheet,
)

PINK = (240, 80, 160, 255)


def _packed_pink_grid(rows: int, cols: int) -> Image.Image:
    """A gap-less sheet on a non-#FF00FF (pink) background — defeats chroma slicing."""
    cell = 100
    sheet = Image.new("RGBA", (cols * cell, rows * cell), PINK)
    draw = ImageDraw.Draw(sheet)
    for r in range(rows):
        for c in range(cols):
            x, y = c * cell + 20, r * cell + 20
            draw.ellipse([x, y, x + 60, y + 60], fill=(0, 100 + r * 20, 200, 255))
    return sheet


MAGENTA = (255, 0, 255, 255)


def _make_sheet() -> Image.Image:
    sheet = Image.new("RGBA", (320, 200), MAGENTA)
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([10, 10, 90, 90], fill=(0, 128, 255, 255))  # blue square
    draw.rectangle([130, 10, 210, 90], fill=(0, 200, 0, 255))  # green square
    draw.rectangle([240, 10, 300, 190], fill=(220, 220, 0, 255))  # tall yellow
    return sheet


def test_chroma_key_makes_background_transparent() -> None:
    sheet = _make_sheet()
    keyed = chroma_key(sheet)
    arr = np.asarray(keyed)
    assert arr[0, 0, 3] == 0  # corner (magenta) -> transparent
    assert arr[50, 50, 3] == 255  # inside blue square -> opaque


def test_despill_reduces_magenta_fringe() -> None:
    img = Image.new("RGBA", (2, 1), MAGENTA)
    img.putpixel((1, 0), (255, 100, 255, 255))  # magenta-tinted foreground
    keyed = chroma_key(img, tolerance=40.0)
    arr = np.asarray(keyed)
    assert arr[0, 0, 3] == 0  # pure magenta keyed out
    assert arr[0, 1, 3] == 255  # foreground kept
    assert arr[0, 1, 0] < 255  # red pulled down by despill
    assert arr[0, 1, 2] < 255  # blue pulled down by despill


def test_slice_sheet_finds_all_components() -> None:
    keyed = chroma_key(_make_sheet())
    pieces = slice_sheet(keyed)
    assert len(pieces) == 3
    # ordered left-to-right on the same row
    widths = [p.size[0] for p in pieces]
    assert widths[0] > 0


def test_fit_to_512_one_side_exact_no_distortion() -> None:
    fitted = fit_to_512(Image.new("RGBA", (100, 40)))
    assert max(fitted.size) == 512
    # aspect ratio preserved (100:40 == 5:2)
    w, h = fitted.size
    assert abs(w / h - 100 / 40) < 0.02


def test_fit_to_512_tall_image() -> None:
    fitted = fit_to_512(Image.new("RGBA", (40, 100)))
    assert fitted.size[1] == 512


def test_encode_sticker_under_limit() -> None:
    data = encode_sticker(Image.new("RGBA", (512, 512), (10, 20, 30, 255)))
    assert data.startswith(b"\x89PNG")
    assert len(data) <= 512 * 1024


def test_process_sheet_end_to_end() -> None:
    buffer = BytesIO()
    _make_sheet().save(buffer, format="PNG")
    stickers = process_sheet(buffer.getvalue())

    assert len(stickers) == 3
    for data in stickers:
        assert len(data) <= 512 * 1024
        img = Image.open(BytesIO(data))
        assert max(img.size) == 512  # one side exactly 512
        assert img.mode == "RGBA"


def test_process_sheet_accepts_pil_image() -> None:
    stickers = process_sheet(_make_sheet())
    assert len(stickers) == 3


def test_min_area_filters_specks() -> None:
    sheet = Image.new("RGBA", (100, 100), MAGENTA)
    sheet.putpixel((50, 50), (0, 0, 0, 255))  # 1px speck
    keyed = chroma_key(sheet)
    assert slice_sheet(keyed, min_area=256) == []


# --- grid fallback ----------------------------------------------------------


def test_grid_for_balanced() -> None:
    assert grid_for(6) == (2, 3)
    assert grid_for(15) == (4, 4)
    assert grid_for(1) == (1, 1)


def test_chroma_key_auto_detects_pink_bg() -> None:
    keyed = chroma_key_auto(_packed_pink_grid(1, 1))
    arr = np.asarray(keyed)
    assert arr[0, 0, 3] == 0  # pink corner detected and removed
    assert arr[50, 50, 3] == 255  # the circle stays


def test_grid_slice_separates_packed_cells() -> None:
    pieces = grid_slice(_packed_pink_grid(2, 3), 2, 3)
    assert len(pieces) == 6
    for piece in pieces:
        assert np.asarray(piece.convert("RGBA"))[..., 3].max() == 255


def test_process_sheet_grid_fallback_when_chroma_fails() -> None:
    # Pink, gap-less sheet: chroma #FF00FF yields 1 blob; grid fallback recovers 6.
    sheet = _packed_pink_grid(2, 3)
    assert len(slice_sheet(chroma_key(sheet))) < 6  # chroma path under-performs
    stickers = process_sheet(sheet, grid=(2, 3))
    assert len(stickers) == 6
    for data in stickers:
        assert max(Image.open(BytesIO(data)).size) == 512
