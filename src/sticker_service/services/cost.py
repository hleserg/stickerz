"""Cost model for the image pipeline — used by the A/B comparison harness (HLE-1040).

Image output dominates (~85–90%), but input reference images and vision calls
are not zero, so we break the per-pack cost into those three lines. Prices are
the June-2026 estimates from HLE-1040 (USD; ₽ at ~73.5/$). Treat as estimates —
the harness reports them alongside measured call counts.
"""

from __future__ import annotations

from dataclasses import dataclass

USD_RUB = 73.5

# USD per generated image, keyed by (model label, resolution).
IMAGE_USD: dict[tuple[str, str], float] = {
    ("pro", "1K"): 0.134,
    ("pro", "2K"): 0.134,
    ("pro", "4K"): 0.24,
    ("flash", "1K"): 0.067,
    ("flash", "2K"): 0.10,
    ("flash", "4K"): 0.15,
}
VISION_USD = 0.002  # per vision call (analyse/gate/emoji) — rough estimate
INPUT_REF_USD = 0.001  # per input reference image attached to a generation


def image_cost(model: str, resolution: str) -> float:
    """USD for one generated image at (model, resolution). Unknown → pro/2K."""
    return IMAGE_USD.get((model, resolution), IMAGE_USD[("pro", "2K")])


@dataclass(frozen=True)
class CostBreakdown:
    """Per-run cost split into the three lines we care about."""

    image_usd: float
    input_usd: float
    vision_usd: float

    @property
    def total_usd(self) -> float:
        return self.image_usd + self.input_usd + self.vision_usd

    @property
    def total_rub(self) -> float:
        return self.total_usd * USD_RUB

    def as_row(self) -> dict[str, float]:
        """Flat dict for a metrics table."""
        return {
            "image_usd": round(self.image_usd, 4),
            "input_usd": round(self.input_usd, 4),
            "vision_usd": round(self.vision_usd, 4),
            "total_usd": round(self.total_usd, 4),
            "total_rub": round(self.total_rub, 1),
        }


def breakdown(
    *, image_calls: list[tuple[str, str]], input_refs: int, vision_calls: int
) -> CostBreakdown:
    """Cost of a run from its measured calls.

    ``image_calls`` is one (model, resolution) per generated image; ``input_refs``
    the total reference images sent in; ``vision_calls`` the analyse/gate/emoji passes.
    """
    return CostBreakdown(
        image_usd=sum(image_cost(m, r) for m, r in image_calls),
        input_usd=input_refs * INPUT_REF_USD,
        vision_usd=vision_calls * VISION_USD,
    )
