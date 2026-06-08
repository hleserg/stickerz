"""Compose sliced stickers into a transparent preview sheet for review (pre-publish).

Tiles the cut-out stickers (transparent background) into a grid image so the user
can eyeball the whole pack before it's published to Telegram. When more than
``per_sheet`` stickers are shown they are split across several sheets — **evenly**
(half/half, ±1 for odd counts) and on **identically sized canvases**, so every
sticker renders at the same visual scale and no leftover sheet looks oversized.
"""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image

from sticker_service.services.postprocess.slice_stickers import fit_to_512

_PER_SHEET = 12
_COLS = 3
_CELL = 256
_PAD = 14


# PLAYBOOK-START
# pattern: balanced-pagination-with-uniform-canvas
# status: draft
# problem: greedily filling pages to a cap leaves a tiny last page whose few
#   items render oversized next to the packed pages — visually inconsistent.
# solution: fix the page COUNT first (ceil(n / cap)), spread items evenly across
#   it (base + 1 for the first `remainder` pages), and size every page to the
#   largest page's grid so all pages share dimensions and thus the same scale.
def _balanced_sizes(total: int, per_sheet: int) -> list[int]:
    """Split ``total`` items across ceil(total/per_sheet) sheets as evenly as possible."""
    count = max(1, math.ceil(total / per_sheet))
    base, extra = divmod(total, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def compose_preview(
    stickers: list[bytes],
    *,
    per_sheet: int = _PER_SHEET,
    cols: int = _COLS,
    cell: int = _CELL,
    pad: int = _PAD,
) -> list[bytes]:
    """Tile stickers onto transparent preview sheets (PNG), one per sheet.

    Sheets are balanced (≈equal counts) and all share the largest sheet's
    dimensions, so a sticker looks the same size on every preview.
    """
    if not stickers:
        return []
    sizes = _balanced_sizes(len(stickers), per_sheet)
    # Uniform canvas: size every sheet to the busiest one so all render alike.
    rows = max(1, math.ceil(sizes[0] / cols))
    width = pad + cols * (cell + pad)
    height = pad + rows * (cell + pad)

    sheets: list[bytes] = []
    offset = 0
    for size in sizes:
        chunk = stickers[offset : offset + size]
        offset += size
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        for i, data in enumerate(chunk):
            tile = fit_to_512(Image.open(BytesIO(data)), side=cell)
            r, c = divmod(i, cols)
            x = pad + c * (cell + pad) + (cell - tile.width) // 2
            y = pad + r * (cell + pad) + (cell - tile.height) // 2
            canvas.paste(tile, (x, y), tile)
        buffer = BytesIO()
        canvas.save(buffer, format="PNG")
        sheets.append(buffer.getvalue())
    return sheets
