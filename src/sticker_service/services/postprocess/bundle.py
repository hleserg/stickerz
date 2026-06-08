"""Bundle finished stickers into a downloadable ZIP (Telegram-ready PNGs)."""

from __future__ import annotations

import zipfile
from io import BytesIO


def bundle_zip(stickers: list[bytes]) -> bytes:
    """Zip sliced 512px PNG stickers into a single archive for download."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, data in enumerate(stickers):
            zf.writestr(f"sticker_{i:02d}.png", data)
    return buffer.getvalue()
