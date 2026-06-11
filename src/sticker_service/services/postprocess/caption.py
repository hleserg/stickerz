"""Deterministic caption overlay for stickers.

The model is forbidden from drawing ANY text (one blunt rule even a weak
fallback model follows); captions are drawn HERE in post-processing instead.
That makes the owner's rule exact and unbreakable: text appears only where the
item is a spoken line (replica) or the user explicitly quoted a caption —
pixel-perfect Cyrillic, no stray quote marks, no parenthesized stage
directions, no labels on emotions.
"""

from __future__ import annotations

import re
from io import BytesIO

from PIL import Image, ImageDraw

from sticker_service.services.postprocess.slice_stickers import encode_sticker
from sticker_service.services.postprocess.watermark import _font

# «…», "…", „…“ — any quoted fragment of a custom idea is an explicit caption.
_QUOTED_RE = re.compile(r'[«"„]([^»"“]+)[»"“]')


# PLAYBOOK-START
# id: deterministic-overlay-over-model-lettering
# title: Render must-be-exact artifacts in code, not by the image model
# status: draft
# category: reliability
# tags: [genai, postprocess, determinism]
# When part of a generated image must be EXACT (captions, logos, UI chrome),
# don't prompt the model to draw it — models (especially fallback tiers) break
# format rules under load. Forbid the model from drawing that element entirely
# and composite it deterministically in post-processing. Substitution test
# passes: applies to any generated-asset pipeline, not just stickers.
# PLAYBOOK-END
def caption_text_for(item: str) -> str | None:
    """The exact text to overlay for one sheet item, or None for no caption.

    A standard replica maps to its curated caption; a custom idea yields its
    first quoted fragment; everything else (emotions, plain descriptions) gets
    no text at all.
    """
    from sticker_service.services.stickers.sets import STANDARD_REPLICAS

    item = item.strip()
    if item in STANDARD_REPLICAS:
        return STANDARD_REPLICAS[item]
    match = _QUOTED_RE.search(item)
    if match:
        text = match.group(1).strip()
        return text or None
    return None


def _wrap(text: str, limit: int = 16) -> list[str]:
    """Greedy two-line wrap so long captions stay legible at chat size."""
    words = text.split()
    if not words:
        return []
    lines = [words[0]]
    for word in words[1:]:
        if len(lines[-1]) + 1 + len(word) <= limit:
            lines[-1] += f" {word}"
        else:
            lines.append(word)
    if len(lines) > 2:  # squeeze the tail into line 2 rather than overflow
        lines = [lines[0], " ".join(lines[1:])]
    return lines


def draw_caption(sticker: bytes, text: str) -> bytes:
    """Draw ``text`` along the bottom of a sticker; returns PNG bytes.

    Classic sticker lettering: bold white with a dark stroke, centred, sized
    for chat rendering (~160px), wrapped to two lines max. Sits above the
    watermark strip so the two never collide.
    """
    img = Image.open(BytesIO(sticker)).convert("RGBA")
    w, h = img.size
    lines = _wrap(text)
    if not lines:
        return sticker
    size = max(24, int(h * 0.085))
    font = _font(size)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    stroke = max(2, size // 12)
    line_heights = []
    widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
        widths.append(bbox[2] - bbox[0])
        line_heights.append(bbox[3] - bbox[1])
    total_h = sum(line_heights) + (len(lines) - 1) * 4
    # Bottom block, leaving ~7% for the watermark strip below.
    y = h - int(h * 0.07) - total_h
    for line, lw, lh in zip(lines, widths, line_heights, strict=True):
        draw.text(
            ((w - lw) // 2, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=stroke,
            stroke_fill=(35, 35, 35, 255),
        )
        y += lh + 4
    return encode_sticker(Image.alpha_composite(img, layer))
