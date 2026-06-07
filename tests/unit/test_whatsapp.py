"""Tests for WhatsApp repackaging (WebP 512×512 ≤100 KB, §7)."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from sticker_service.services.postprocess import to_whatsapp, to_whatsapp_pack


def _png(size: tuple[int, int], color: tuple[int, int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_to_whatsapp_is_512_square_webp_under_100kb() -> None:
    data = to_whatsapp(_png((400, 200), (10, 200, 50, 255)))
    img = Image.open(BytesIO(data))
    assert img.format == "WEBP"
    assert img.size == (512, 512)
    assert len(data) <= 100 * 1024


def test_to_whatsapp_pads_non_square_without_distortion() -> None:
    # A wide image stays wide inside the square (centered, padded).
    data = to_whatsapp(_png((400, 100), (0, 0, 255, 255)))
    img = Image.open(BytesIO(data)).convert("RGBA")
    assert img.size == (512, 512)
    # Corners are transparent padding.
    corner = img.getpixel((0, 0))
    assert isinstance(corner, tuple)
    assert corner[3] == 0


def test_to_whatsapp_accepts_pil_image() -> None:
    data = to_whatsapp(Image.new("RGBA", (512, 512), (1, 2, 3, 255)))
    assert Image.open(BytesIO(data)).size == (512, 512)


def test_to_whatsapp_pack_maps_all() -> None:
    pack = to_whatsapp_pack([_png((100, 100), (255, 0, 0, 255)) for _ in range(3)])
    assert len(pack) == 3
    for data in pack:
        assert len(data) <= 100 * 1024
