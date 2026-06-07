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


def test_compose_preview_splits_into_sheets_of_15() -> None:
    sheets = compose_preview([_png() for _ in range(16)])
    assert len(sheets) == 2  # 15 + 1


def test_compose_preview_empty() -> None:
    assert compose_preview([]) == []
