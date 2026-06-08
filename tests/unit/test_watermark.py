"""Tests for the bottom watermark (HLE-1043)."""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image

from sticker_service.services.postprocess import apply_watermark


def _sticker_png() -> bytes:
    """A 512px sticker with an opaque silhouette in the upper-middle area."""
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    arr = np.asarray(img).copy()
    arr[80:360, 150:360] = (0, 120, 200, 255)  # the 'character'
    buf = BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def test_watermark_adds_pixels_at_bottom_and_keeps_512() -> None:
    src = _sticker_png()
    out = apply_watermark(src, text="@yuki_stickers_bot")
    before = np.asarray(Image.open(BytesIO(src)).convert("RGBA"))[..., 3]
    after = np.asarray(Image.open(BytesIO(out)).convert("RGBA"))
    assert max(after.shape[:2]) == 512  # still a valid 512 sticker
    # new opaque pixels appeared in the lower band (the watermark text)
    band = slice(360, 512)
    assert after[band, :, 3].sum() > before[band, :].sum()


def test_watermark_size_bounded() -> None:
    out = apply_watermark(_sticker_png())
    assert len(out) <= 512 * 1024  # respects the sticker size limit


def test_watermark_text_is_configurable() -> None:
    # Different text → different output bytes (text actually rendered).
    a = apply_watermark(_sticker_png(), text="@yuki_stickers_bot")
    b = apply_watermark(_sticker_png(), text="@other_bot")
    assert a != b
