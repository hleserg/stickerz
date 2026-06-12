"""Bottom watermark for stickers (HLE-1043).

The "tiny_bottom" style chosen in review: a small, light, centred caption sitting
just under the figure on the lower edge — readable but unobtrusive, never over the
face. The handle gets its own clear band below the art (grow the canvas to the
512 cap, else shrink the art), so it never covers a caption baked into the art
and the caption itself rises clear of Telegram's chat timestamp overlay.
Applied to every sticker after slicing; configurable (on/off + text) so a
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
#: Telegram's hard cap — and the exact length the longest side must keep.
_TELEGRAM_SIDE = 512


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (_FONT_FILE, Path(_SYSTEM_FONT)):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()  # pragma: no cover - last-resort, ugly but never crashes


def _silhouette_bottom(alpha: np.ndarray) -> int:
    rows = np.where(alpha.max(axis=1) > 8)[0]
    return int(rows.max()) if rows.size else alpha.shape[0] - 1


# PLAYBOOK-START
# id: reserve-overlay-band
# title: Reserve a clear band for overlays instead of clamping them onto content
# status: draft
# category: media
# tags: [imaging, layout]
# When an overlay (badge, watermark, the platform's own timestamp chrome) must
# never cover content but the content is a tight crop filling the canvas, do
# not clamp the overlay inward — make room: grow the canvas toward the
# platform's size cap first (free), then uniformly shrink the content for the
# remainder. Works for any fixed-cap canvas (stickers, avatars, thumbnails).
# PLAYBOOK-END
def _clear_bottom_band(img: Image.Image, band: int) -> Image.Image:
    """Return ``img`` with ≥ ``band`` transparent px below the silhouette.

    Slices are tight crops (``fit_to_512``), so the art — including any caption
    baked into it — usually runs to the bottom edge: there is no room "below
    the figure" and the old clamp drew the handle ON the caption (and Telegram
    overlays its chat timestamp on the same bottom edge). Grow the canvas
    toward Telegram's 512 cap first (free); only when the canvas is already at
    the cap, shrink the art uniformly to clear the band.
    """
    w, h = img.size
    bottom = _silhouette_bottom(np.asarray(img)[..., 3])
    deficit = bottom + 1 + band - h
    if deficit <= 0:
        return img
    grow = min(deficit, _TELEGRAM_SIDE - h)
    art = img
    if deficit > grow:  # canvas at the cap — the art itself must shrink
        factor = (h + grow - band) / (bottom + 1)
        art = img.resize(
            (max(1, round(w * factor)), max(1, round(h * factor))),
            Image.Resampling.LANCZOS,
        )
    canvas = Image.new("RGBA", (w, h + grow), (0, 0, 0, 0))
    canvas.paste(art, ((w - art.width) // 2, 0))
    return canvas


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
    size = max(10, int(img.height * scale))
    font = _font(size)
    stroke = 2
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = probe.textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw, th = int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])
    gap = max(3, th // 3)
    img = _clear_bottom_band(img, gap + th + 4)
    w, h = img.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    bottom = _silhouette_bottom(np.asarray(img)[..., 3])
    # Just BELOW the figure (owner: the handle must not cover the art), with a
    # small gap; the band reserved above guarantees the room, the clamp stays
    # as a belt-and-braces bound.
    y = min(h - th - 2, bottom + gap)
    draw.text(
        ((w - tw) // 2, y),
        text,
        font=font,
        fill=(255, 255, 255, opacity),
        stroke_width=stroke,
        stroke_fill=(45, 45, 45, min(255, opacity + 50)),
    )
    return encode_sticker(Image.alpha_composite(img, layer))
