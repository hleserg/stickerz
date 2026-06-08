"""Tests for the model/resolution fallback ladder (HLE-1055)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from sticker_service.services.models.base import (
    ImageModel,
    ModelError,
    ModelQuotaError,
    ModelRefusalError,
    generate_via_ladder,
)

LADDER = (("pro", "4K"), ("pro", "2K"), ("flash", "4K"))


class _ScriptedModel(ImageModel):
    """Records (model, size) attempts and raises per a scripted plan."""

    name = "scripted"

    def __init__(self, plan: list[object]) -> None:
        self._plan = plan
        self.calls: list[tuple[str | None, str | None]] = []

    async def generate(
        self, prompt: str, refs: Sequence[bytes] = (), *, model=None, image_size=None
    ) -> bytes:
        self.calls.append((model, image_size))
        outcome = self._plan[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:  # pragma: no cover
        return 1.0

    async def pick_emoji(self, image: bytes) -> str:  # pragma: no cover
        return "🙂"


async def test_ladder_falls_over_to_next_rung_on_transient() -> None:
    # rung0 exhausts transient (raises ModelError) → rung1 succeeds.
    model = _ScriptedModel([ModelError("503"), b"\x89PNGok"])
    out = await generate_via_ladder(model, "p", [b"r"], LADDER)
    assert out == b"\x89PNGok"
    assert model.calls == [("pro", "4K"), ("pro", "2K")]  # dropped one rung


async def test_ladder_fails_fast_on_quota() -> None:
    model = _ScriptedModel([ModelQuotaError("credits depleted")])
    with pytest.raises(ModelQuotaError):
        await generate_via_ladder(model, "p", [b"r"], LADDER)
    assert len(model.calls) == 1  # no failover attempts


async def test_ladder_reformulates_then_gives_up_on_refusal() -> None:
    # Always refuses: tries each reformulation on rung0, then stops (no failover).
    model = _ScriptedModel([ModelRefusalError("safety")] * 5)
    with pytest.raises(ModelRefusalError):
        await generate_via_ladder(model, "p", [b"r"], LADDER, reformulations=("", " a", " b"))
    assert model.calls == [("pro", "4K"), ("pro", "4K"), ("pro", "4K")]  # one rung, 3 nudges
