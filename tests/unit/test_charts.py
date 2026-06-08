"""Tests for the admin stats infographic."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from sticker_service.services.charts import render_bar_chart


def test_render_bar_chart_returns_png() -> None:
    data = render_bar_chart("Funnel", [("Starts", 10), ("Published", 3), ("Errors", 0)])
    img = Image.open(BytesIO(data))
    assert img.format == "PNG"
    assert img.size[0] > 0 and img.size[1] > 0


def test_render_bar_chart_empty() -> None:
    data = render_bar_chart("Empty", [])
    assert Image.open(BytesIO(data)).format == "PNG"
