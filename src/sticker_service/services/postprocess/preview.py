"""Compose sliced stickers into a transparent preview sheet for review (pre-publish).

Tiles the cut-out stickers (transparent background) into a grid image so the user
can eyeball the whole pack before it's published to Telegram. Up to ``per_sheet``
stickers per image; spills onto additional sheets if there are more.
"""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image

from sticker_service.services.postprocess.slice_stickers import fit_to_512

_PER_SHEET = 15
_COLS = 5
_CELL = 256
_PAD = 14


def compose_preview(
    stickers: list[bytes],
    *,
    per_sheet: int = _PER_SHEET,
    cols: int = _COLS,
    cell: int = _CELL,
    pad: int = _PAD,
) -> list[bytes]:
    """Tile stickers onto transparent preview sheets (PNG). Returns one per sheet."""
    sheets: list[bytes] = []
    for start in range(0, len(stickers), per_sheet):
        chunk = stickers[start : start + per_sheet]
        rows = max(1, math.ceil(len(chunk) / cols))
        width = pad + cols * (cell + pad)
        height = pad + rows * (cell + pad)
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
