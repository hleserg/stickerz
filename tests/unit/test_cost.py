"""Tests for the image-pipeline cost model (A/B harness, HLE-1040)."""

from __future__ import annotations

from sticker_service.services import cost


def test_image_cost_lookup_and_default() -> None:
    assert cost.image_cost("flash", "2K") == 0.10
    assert cost.image_cost("pro", "4K") == 0.24
    assert cost.image_cost("pro", "1K") == 0.134
    # Unknown combo falls back to pro/2K rather than raising.
    assert cost.image_cost("???", "8K") == cost.IMAGE_USD[("pro", "2K")]


def test_breakdown_splits_three_lines() -> None:
    b = cost.breakdown(
        image_calls=[("flash", "2K"), ("flash", "2K"), ("flash", "2K")],  # 3 canonical steps
        input_refs=3,  # one reference per step
        vision_calls=4,  # gate + emoji passes
    )
    assert round(b.image_usd, 3) == 0.30
    assert round(b.input_usd, 3) == 0.003
    assert round(b.vision_usd, 3) == 0.008
    assert round(b.total_usd, 3) == 0.311
    assert round(b.total_rub, 1) == round(0.311 * cost.USD_RUB, 1)


def test_breakdown_as_row_is_flat() -> None:
    row = cost.breakdown(image_calls=[("pro", "4K")], input_refs=1, vision_calls=0).as_row()
    assert set(row) == {"image_usd", "input_usd", "vision_usd", "total_usd", "total_rub"}
    assert row["image_usd"] == 0.24
