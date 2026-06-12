"""Tests for the bottom watermark (HLE-1043)."""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image, ImageDraw

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


def test_watermark_is_chat_legible() -> None:
    # HLE-1060: the handle must survive Telegram's ~160px chat downscale. The
    # default rendering lays down ≥2× the ink of the old illegible 0.034/150
    # setting — pinned so a "tasteful" tweak can't quietly shrink it again.
    src = _sticker_png()

    def ink(png: bytes) -> int:
        base = np.asarray(Image.open(BytesIO(src)).convert("RGBA"))[..., 3].astype(int)
        out = np.asarray(Image.open(BytesIO(png)).convert("RGBA"))[..., 3].astype(int)
        return int((out - base).clip(min=0).sum())

    old = apply_watermark(src, opacity=150, scale=0.034)
    new = apply_watermark(src)
    assert ink(new) >= 2 * ink(old)


def test_watermark_text_is_configurable() -> None:
    # Different text → different output bytes (text actually rendered).
    a = apply_watermark(_sticker_png(), text="@yuki_stickers_bot")
    b = apply_watermark(_sticker_png(), text="@other_bot")
    assert a != b


def _rows_of(mask: np.ndarray) -> np.ndarray:
    return np.where(mask.any(axis=1))[0]


def test_watermark_clears_a_full_height_sticker() -> None:
    # Regression (owner screenshot): slices are tight crops, so a caption baked
    # into the art runs to the bottom edge and the handle landed ON it (with
    # Telegram's chat timestamp on top). The art must shrink to clear a bottom
    # band; the handle sits strictly below every art pixel.
    img = Image.new("RGBA", (400, 512), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([60, 0, 340, 511], fill=(40, 90, 200, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    out = np.asarray(Image.open(BytesIO(apply_watermark(buf.getvalue()))).convert("RGBA"))
    assert max(out.shape[:2]) == 512  # the longest side still meets Telegram's rule
    blue = (out[..., 2] > 150) & (out[..., 0] < 100) & (out[..., 3] > 0)
    white = (out[..., :3] > 200).all(axis=-1) & (out[..., 3] > 0)
    assert white.any()  # watermark drawn
    assert _rows_of(blue).max() < _rows_of(white).min()  # strictly below the art


def test_watermark_grows_a_wide_sticker_canvas_for_free() -> None:
    # A wide slice has headroom below the 512 cap: the canvas grows downward
    # and the art keeps its exact pixels instead of shrinking.
    img = Image.new("RGBA", (512, 300), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([0, 0, 511, 299], fill=(40, 90, 200, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    out_img = Image.open(BytesIO(apply_watermark(buf.getvalue()))).convert("RGBA")
    assert out_img.width == 512 and 300 < out_img.height <= 512
    out = np.asarray(out_img)
    blue = (out[..., 2] > 150) & (out[..., 0] < 100) & (out[..., 3] > 0)
    white = (out[..., :3] > 200).all(axis=-1) & (out[..., 3] > 0)
    assert int(blue[150].sum()) == 512  # art untouched, full original width
    assert _rows_of(blue).max() == 299
    assert _rows_of(white).min() > 299  # handle in the new band below


def test_watermark_sits_below_the_figure() -> None:
    # Owner's rule: the handle must not cover the art — it goes BELOW the
    # silhouette bottom (clamped to the canvas).
    from io import BytesIO

    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse([100, 50, 400, 300], fill=(40, 90, 200, 255))  # figure top half
    buf = BytesIO()
    img.save(buf, format="PNG")
    out = Image.open(BytesIO(apply_watermark(buf.getvalue())))
    arr = np.asarray(out)
    figure_bottom = 300
    added = (arr[..., 3] > 0) & ~(np.asarray(img.convert("RGBA"))[..., 3] > 0)
    rows = np.where(added.any(axis=1))[0]
    assert rows.size  # watermark drawn
    assert rows.min() > figure_bottom  # strictly below the figure
