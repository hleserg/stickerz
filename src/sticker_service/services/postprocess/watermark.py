"""Bottom watermark for stickers (HLE-1043).

The "tiny_bottom" style chosen in review: a small, light, centred caption sitting
just under the figure on the lower edge — readable but unobtrusive, never over the
face. Applied to every sticker after slicing; configurable (on/off + text) so a
B2B/no-watermark build is a flag flip. The font is bundled so it works in the slim
Docker image (no system fonts).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from sticker_service.services.postprocess.slice_stickers import encode_sticker

DEFAULT_TEXT = "@yuki_stickers_bot"
_FONT_FILE = Path(__file__).resolve().parents[2] / "assets" / "DejaVuSans-Bold.ttf"
_SYSTEM_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (_FONT_FILE, Path(_SYSTEM_FONT)):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()  # pragma: no cover - last-resort, ugly but never crashes


def _silhouette_bottom(alpha: np.ndarray) -> int:
    rows = np.where(alpha.max(axis=1) > 8)[0]
    return int(rows.max()) if rows.size else alpha.shape[0] - 1


def apply_watermark(
    sticker: bytes, *, text: str = DEFAULT_TEXT, opacity: int = 205, scale: float = 0.055
) -> bytes:
    """Overlay the watermark along the bottom of a 512px sticker; return PNG bytes.

    Sized for the CHAT, not the editor: Telegram renders stickers at ~160px in
    a conversation, so the 512px master needs ~28px text (scale 0.055) for the
    handle to stay legible after downscaling — the old 0.034/150 setting shrank
    to ~5px and read as noise (HLE-1060).
    """
    img = Image.open(BytesIO(sticker)).convert("RGBA")
    w, h = img.size
    size = max(10, int(h * scale))
    font = _font(size)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    stroke = 2
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bottom = _silhouette_bottom(np.asarray(img)[..., 3])
    # Just BELOW the figure (owner: the handle must not cover the art), with a
    # small gap; clamped so the text always stays inside the canvas.
    y = min(h - th - 2, bottom + max(3, th // 3))
    draw.text(
        ((w - tw) // 2, y),
        text,
        font=font,
        fill=(255, 255, 255, opacity),
        stroke_width=stroke,
        stroke_fill=(45, 45, 45, min(255, opacity + 50)),
    )
    return encode_sticker(Image.alpha_composite(img, layer))
