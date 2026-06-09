"""Tests for chroma-key slicing and 512 fitting (§7, §B.4)."""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw

from sticker_service.services.postprocess import (
    chroma_key,
    chroma_key_auto,
    drop_outlier_fragments,
    drop_text_strips,
    encode_sticker,
    fit_to_512,
    grid_for,
    grid_slice,
    process_sheet,
    slice_sheet,
)
from sticker_service.services.postprocess.slice_stickers import _clean_satellites, _opaque_area

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
    # magenta | blue buffer | magenta-tinted foreground. The buffer keeps the tint
    # from being absorbed by the hysteresis (it's a kept pixel), so despill applies.
    img = Image.new("RGBA", (3, 1), MAGENTA)
    img.putpixel((1, 0), (0, 0, 255, 255))  # opaque blue buffer (not magenta-ish)
    img.putpixel((2, 0), (255, 100, 255, 255))  # magenta-tinted foreground
    keyed = chroma_key(img, tolerance=40.0)
    arr = np.asarray(keyed)
    assert arr[0, 0, 3] == 0  # pure magenta keyed out
    assert arr[0, 2, 3] == 255  # foreground kept
    assert arr[0, 2, 0] < 255  # red pulled down by despill
    assert arr[0, 2, 2] < 255  # blue pulled down by despill


def test_hysteresis_absorbs_connected_wash_keeps_isolated_speck() -> None:
    # A pink/purple wash (magenta-ish but outside the strict tolerance) bleeding off
    # the magenta background must be removed; an isolated magenta speck *on* the
    # figure (not connected to the background) must stay. (The user's "smear behind
    # the contour" artifact.)
    img = Image.new("RGBA", (20, 20), MAGENTA)
    draw = ImageDraw.Draw(img)
    draw.rectangle([5, 5, 14, 14], fill=(0, 0, 200, 255))  # blue figure
    # Wash strip from the figure to the top border → connected to the magenta sea.
    draw.rectangle([9, 0, 10, 5], fill=(230, 80, 210, 255))
    img.putpixel((10, 10), (235, 70, 215, 255))  # isolated magenta-ish speck inside figure
    arr = np.asarray(chroma_key(img))
    assert arr[0, 0, 3] == 0  # background corner transparent
    assert arr[2, 9, 3] == 0  # wash absorbed (was magenta-ish, connected to bg)
    assert arr[10, 10, 3] == 255  # isolated speck on the figure kept
    assert arr[10, 7, 3] == 255  # figure body kept


def test_family_flood_removes_light_pink_wash_keeps_pink_inside_outline() -> None:
    # Prod brak: a LIGHT watercolor wash (euclidean ~186 from #FF00FF — beyond any
    # safe loose tolerance, light pinks read closer to white than to magenta) glued
    # to the figure. The pink-family flood (min(R,B)-G) absorbs it at any lightness,
    # while the white die-cut outline is a wall it cannot cross — the identical
    # pink "clothing" INSIDE the outline survives.
    img = Image.new("RGBA", (30, 20), MAGENTA)
    draw = ImageDraw.Draw(img)
    draw.rectangle([4, 4, 15, 15], fill=(255, 255, 255, 255))  # white die-cut figure
    draw.rectangle([7, 7, 12, 12], fill=(232, 180, 216, 255))  # pink clothing inside
    draw.rectangle([16, 6, 27, 13], fill=(232, 180, 216, 255))  # same pink as a wash
    arr = np.asarray(chroma_key(img))
    assert arr[8, 20, 3] == 0  # the wash is flooded away…
    assert arr[8, 8, 3] == 255  # …the identical pink inside the outline stays
    assert arr[5, 5, 3] == 255  # the white outline itself stays
    assert arr[8, 8, 0] > 0  # clothing keeps colour (despill trims, not erases)


def test_family_flood_spares_red_prop_touching_background() -> None:
    # Red is not pink-family (its blue ≈ green): a red heart sitting straight on
    # the background must NOT be flooded, however warm it looks.
    img = Image.new("RGBA", (12, 12), MAGENTA)
    ImageDraw.Draw(img).rectangle([4, 4, 7, 7], fill=(224, 48, 48, 255))
    arr = np.asarray(chroma_key(img))
    assert arr[5, 5, 3] == 255


def test_wash_no_longer_inflates_sticker_size() -> None:
    # The size brak: a wash glued to the figure inflated the component's bbox, so
    # the character shrank after fitting to 512. Flooded away, the piece is exactly
    # the figure's own box again.
    sheet = Image.new("RGBA", (60, 40), MAGENTA)
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([5, 5, 24, 34], fill=(255, 255, 255, 255))  # 20×30 figure
    draw.rectangle([25, 10, 54, 20], fill=(226, 168, 204, 255))  # wash dragging right
    pieces = slice_sheet(chroma_key(sheet))
    assert len(pieces) == 1
    assert pieces[0].size == (20, 30)


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


def test_drop_text_strips_removes_short_wide_fragments() -> None:
    def blank(w: int, h: int) -> Image.Image:
        return Image.new("RGBA", (w, h), (0, 0, 0, 255))

    # Two character pieces (tall) + one detached caption line (short, wide).
    pieces = [blank(300, 400), blank(280, 420), blank(320, 60)]
    kept = drop_text_strips(pieces)
    assert len(kept) == 2  # the 320×60 text strip is dropped
    assert all(p.size[1] > 100 for p in kept)


def test_drop_text_strips_keeps_uniform_pieces() -> None:
    same = [Image.new("RGBA", (300, 400), (0, 0, 0, 255)) for _ in range(3)]
    assert len(drop_text_strips(same)) == 3  # nothing is an outlier → keep all


def test_process_sheet_drops_text_only_sticker() -> None:
    # A character square per row plus a detached caption strip floating in the gap.
    sheet = Image.new("RGBA", (240, 320), MAGENTA)
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([40, 10, 200, 170], fill=(0, 128, 255, 255))  # tall character
    draw.rectangle([60, 250, 190, 280], fill=(255, 255, 255, 255))  # detached caption line
    stickers = process_sheet(sheet)
    assert len(stickers) == 1  # only the character survives; text-only strip dropped


def _tile_with_satellites() -> Image.Image:
    """A keyed tile: central figure + a heart next to it + a far corner shard."""
    tile = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.rectangle([60, 40, 140, 170], fill=(0, 128, 255, 255))  # figure (10611 px)
    draw.rectangle([142, 90, 160, 108], fill=(255, 0, 0, 255))  # heart, adjacent (361 px)
    draw.rectangle([180, 5, 198, 23], fill=(0, 0, 0, 255))  # picture-frame corner (361 px)
    return tile


def test_clean_satellites_keeps_adjacent_drops_far_corner() -> None:
    cleaned = _clean_satellites(_tile_with_satellites())
    # figure (10611) + adjacent heart (361) kept; far corner shard (361) dropped.
    assert _opaque_area(cleaned) == 10611 + 361
    assert cleaned.size[0] <= 105  # trimmed box excludes the x≥180 corner


def test_clean_satellites_single_component_is_noop() -> None:
    tile = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rectangle([20, 20, 80, 80], fill=(0, 128, 255, 255))
    assert _opaque_area(_clean_satellites(tile)) == _opaque_area(tile)


def test_drop_outlier_fragments_drops_lone_glyph() -> None:
    # Two characters + one tall-but-small detached glyph (a lone «Я»): the glyph
    # is NOT a short-wide strip, so drop_text_strips misses it — area catches it.
    char = Image.new("RGBA", (300, 400), (0, 0, 0, 255))
    glyph = Image.new("RGBA", (60, 250), (0, 0, 0, 255))  # tall, thin, small area
    pieces = [char.copy(), char.copy(), glyph]
    assert len(drop_text_strips(pieces)) == 3  # shape heuristic keeps the glyph
    assert len(drop_outlier_fragments(pieces)) == 2  # area heuristic drops it


def test_drop_outlier_fragments_respects_expected() -> None:
    char = Image.new("RGBA", (300, 400), (0, 0, 0, 255))
    glyph = Image.new("RGBA", (60, 250), (0, 0, 0, 255))
    pieces = [char.copy(), char.copy(), glyph]
    # expected already met by the real characters → never thin below it.
    assert len(drop_outlier_fragments(pieces, expected=3)) == 3


def test_drop_outlier_fragments_caps_to_expected_for_large_extra() -> None:
    # The "7 for 6" bug: a sizeable stray fragment (>40% of the median, so the
    # small-area heuristic can't catch it) must still be dropped when the count
    # is known. The old budget could only remove sub-threshold pieces.
    char = Image.new("RGBA", (300, 400), (0, 0, 0, 255))
    extra = Image.new("RGBA", (220, 300), (0, 0, 0, 255))  # large-ish, not "small"
    pieces = [char.copy() for _ in range(6)] + [extra]
    kept = drop_outlier_fragments(pieces, expected=6)
    assert len(kept) == 6  # capped to expected; the extra (smallest) is dropped


def test_process_sheet_drops_lone_letter_tile() -> None:
    # Two characters in a 1×3 grid plus a lone letter-sized tile in the third cell.
    sheet = Image.new("RGBA", (360, 200), MAGENTA)
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([20, 20, 120, 180], fill=(0, 128, 255, 255))  # character
    draw.rectangle([140, 20, 240, 180], fill=(0, 200, 0, 255))  # character
    draw.rectangle([285, 60, 305, 140], fill=(0, 0, 0, 255))  # lone glyph «Я»
    stickers = process_sheet(sheet, grid=(1, 3), expected=2)
    assert len(stickers) == 2  # the letter-only tile is dropped


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
    assert grid_for(15) == (5, 3)  # 3-wide portrait sheet
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


def test_process_sheet_keeps_clean_chroma_when_expected_met() -> None:
    # 14 well-separated magenta stickers in a 4×4 grid (16 cells): chroma slicing
    # finds all 14, so we must NOT fall back to the grid just because 14 < 16.
    cell = 100
    sheet = Image.new("RGBA", (4 * cell, 4 * cell), MAGENTA)
    draw = ImageDraw.Draw(sheet)
    for i in range(14):
        r, c = divmod(i, 4)
        x, y = c * cell + 20, r * cell + 20
        draw.rectangle([x, y, x + 60, y + 60], fill=(0, 120, 200, 255))
    stickers = process_sheet(sheet, grid=(4, 4), expected=14)
    assert len(stickers) == 14  # clean chroma result kept, no junky grid fallback
