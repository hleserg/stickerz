"""Repackage Telegram stickers for WhatsApp export (§7).

WhatsApp is stricter than Telegram: WebP, exactly 512×512, ≤100 KB. This is an
optional side button, not part of the main flow — the same sliced stickers are
re-encoded: fit to 512 on the long side, pad to a 512×512 transparent square,
then compress WebP until it fits.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from sticker_service.services.postprocess.slice_stickers import fit_to_512

_SIDE = 512
_MAX_BYTES = 100 * 1024


def _square(image: Image.Image) -> Image.Image:
    fitted = fit_to_512(image, side=_SIDE)
    canvas = Image.new("RGBA", (_SIDE, _SIDE), (0, 0, 0, 0))
    offset = ((_SIDE - fitted.width) // 2, (_SIDE - fitted.height) // 2)
    canvas.paste(fitted, offset, fitted)
    return canvas


def to_whatsapp(sticker: bytes | Image.Image, *, max_bytes: int = _MAX_BYTES) -> bytes:
    """Return a 512×512 WebP ≤100 KB for one sticker."""
    image = sticker if isinstance(sticker, Image.Image) else Image.open(BytesIO(sticker))
    square = _square(image.convert("RGBA"))
    data = b""
    for quality in (90, 80, 70, 60, 50, 40, 30, 20):
        buffer = BytesIO()
        square.save(buffer, format="WEBP", quality=quality, method=6)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
    return data  # pragma: no cover - best effort if even q20 is too big


def to_whatsapp_pack(stickers: list[bytes]) -> list[bytes]:
    """Repackage a whole sliced pack for WhatsApp."""
    return [to_whatsapp(s) for s in stickers]
