"""Make a Telegram sticker-set cover (thumbnail) from a sticker.

Telegram wants a small square thumbnail; we render the chosen sticker centered
on a 100×100 transparent canvas as WEBP under 128 KB.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from sticker_service.services.postprocess.slice_stickers import fit_to_512

_SIDE = 100
_MAX_BYTES = 128 * 1024


def make_cover(sticker: bytes, *, side: int = _SIDE, max_bytes: int = _MAX_BYTES) -> bytes:
    """Return a side×side transparent WEBP cover from one sticker PNG."""
    image = Image.open(BytesIO(sticker)).convert("RGBA")
    fitted = fit_to_512(image, side=side)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(fitted, ((side - fitted.width) // 2, (side - fitted.height) // 2), fitted)
    data = b""
    for quality in (95, 90, 80, 70, 60):
        buffer = BytesIO()
        canvas.save(buffer, format="WEBP", quality=quality, method=6)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
    return data  # pragma: no cover - tiny image always fits
