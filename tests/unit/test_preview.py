"""Tests for the transparent pre-publish preview composite."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from sticker_service.services.postprocess import compose_preview


def _png(color: tuple[int, int, int, int] = (10, 120, 200, 255)) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (200, 240), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_compose_preview_single_sheet() -> None:
    sheets = compose_preview([_png() for _ in range(6)])
    assert len(sheets) == 1
    img = Image.open(BytesIO(sheets[0])).convert("RGBA")
    assert img.mode == "RGBA"
    # Top-left padding pixel is transparent (transparent background preserved).
    corner = img.getpixel((0, 0))
    assert isinstance(corner, tuple) and corner[3] == 0


def test_compose_preview_splits_into_two_sheets() -> None:
    sheets = compose_preview([_png() for _ in range(16)])
    assert len(sheets) == 2


def test_balanced_sizes_splits_half_and_half() -> None:
    from sticker_service.services.postprocess.preview import _balanced_sizes

    assert _balanced_sizes(15, 12) == [8, 7]  # odd → ±1
    assert _balanced_sizes(14, 12) == [7, 7]
    assert _balanced_sizes(13, 12) == [7, 6]  # no lonely leftover sheet
    assert _balanced_sizes(12, 12) == [12]  # fits one sheet
    assert _balanced_sizes(6, 12) == [6]
    assert _balanced_sizes(25, 12) == [9, 8, 8]


def test_compose_preview_sheets_share_dimensions() -> None:
    # 15 stickers → 8 + 7 across two sheets of identical size, so a sticker looks
    # the same on both previews (no oversized leftover sheet).
    sheets = compose_preview([_png() for _ in range(15)])
    assert len(sheets) == 2
    sizes = [Image.open(BytesIO(s)).size for s in sheets]
    assert sizes[0] == sizes[1]


def test_compose_preview_empty() -> None:
    assert compose_preview([]) == []


def test_make_cover_is_100_square_webp() -> None:
    from sticker_service.services.postprocess import make_cover

    data = make_cover(_png())
    img = Image.open(BytesIO(data))
    assert img.format == "WEBP"
    assert img.size == (100, 100)
    assert len(data) <= 128 * 1024


def test_bundle_zip_contains_all_stickers() -> None:
    import zipfile

    from sticker_service.services.postprocess import bundle_zip

    data = bundle_zip([_png(), _png((1, 2, 3, 255)), _png((9, 9, 9, 255))])
    with zipfile.ZipFile(BytesIO(data)) as zf:
        names = zf.namelist()
    assert len(names) == 3
    assert all(n.endswith(".png") for n in names)
