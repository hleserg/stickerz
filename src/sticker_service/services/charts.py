"""Tiny PNG infographic (horizontal bars) for admin stats — no extra deps."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

_W = 640
_ROW = 42
_PAD = 24
_LABEL_W = 230
_BAR = (70, 130, 180)
_BG = (255, 255, 255)
_FG = (20, 20, 20)

# PIL's built-in bitmap font has no Cyrillic (labels render as boxes), so load a
# real TrueType face. DejaVu ships with most Linux distros and is installed in
# the runtime image (fonts-dejavu-core); fall back to the bitmap font if absent.
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


@lru_cache(maxsize=2)
def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_bar_chart(title: str, items: list[tuple[str, int]]) -> bytes:
    """Render labelled horizontal bars to a PNG. ``items`` = [(label, value)]."""
    height = _PAD * 2 + 30 + _ROW * max(1, len(items))
    img = Image.new("RGB", (_W, height), _BG)
    draw = ImageDraw.Draw(img)
    draw.text((_PAD, _PAD), title, fill=_FG, font=_font(18))
    label_font = _font(15)
    max_value = max((v for _, v in items), default=1) or 1
    for i, (label, value) in enumerate(items):
        y = _PAD + 30 + i * _ROW
        bar_w = int((_W - _LABEL_W - _PAD) * value / max_value)
        draw.text((_PAD, y + 4), label[:28], fill=_FG, font=label_font)
        draw.rectangle([_LABEL_W, y, _LABEL_W + bar_w, y + 22], fill=_BAR)
        draw.text((_LABEL_W + bar_w + 6, y + 4), str(value), fill=_FG, font=label_font)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
