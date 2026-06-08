"""Tiny PNG infographic (horizontal bars) for admin stats — no extra deps."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw

_W = 640
_ROW = 42
_PAD = 24
_LABEL_W = 230
_BAR = (70, 130, 180)
_BG = (255, 255, 255)
_FG = (20, 20, 20)


def render_bar_chart(title: str, items: list[tuple[str, int]]) -> bytes:
    """Render labelled horizontal bars to a PNG. ``items`` = [(label, value)]."""
    height = _PAD * 2 + 30 + _ROW * max(1, len(items))
    img = Image.new("RGB", (_W, height), _BG)
    draw = ImageDraw.Draw(img)
    draw.text((_PAD, _PAD), title, fill=_FG)
    max_value = max((v for _, v in items), default=1) or 1
    for i, (label, value) in enumerate(items):
        y = _PAD + 30 + i * _ROW
        bar_w = int((_W - _LABEL_W - _PAD) * value / max_value)
        draw.text((_PAD, y + 6), label[:28], fill=_FG)
        draw.rectangle([_LABEL_W, y, _LABEL_W + bar_w, y + 22], fill=_BAR)
        draw.text((_LABEL_W + bar_w + 6, y + 6), str(value), fill=_FG)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()
