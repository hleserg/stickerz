"""Tests for alpha pricing: action costs and pack formatting."""

from __future__ import annotations

from sticker_service.services import pricing


def test_cost_for_mode() -> None:
    assert pricing.cost_for_mode("fresh") == pricing.COST_NEW_PACK  # 1 pack
    assert pricing.cost_for_mode("reuse") == pricing.COST_ADD_STICKERS  # 0.5 pack
    assert pricing.cost_for_mode("extend") == pricing.COST_ADD_STICKERS
    assert pricing.cost_for_mode("???") == pricing.COST_NEW_PACK  # safe default


def test_add_is_half_of_new() -> None:
    assert pricing.COST_ADD_STICKERS * 2 == pricing.COST_NEW_PACK


def test_format_packs() -> None:
    assert pricing.format_packs(6) == "3"
    assert pricing.format_packs(5) == "2.5"
    assert pricing.format_packs(1) == "0.5"
    assert pricing.format_packs(0) == "0"
