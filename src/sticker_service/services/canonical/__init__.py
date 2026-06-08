"""Canonical pipeline: style schema/loader, the engine, and the gate."""

from __future__ import annotations

from sticker_service.services.canonical.engine import (
    CanonicalEngine,
    CanonicalError,
    build_age_clause,
)
from sticker_service.services.canonical.gate import GateResult, run_gate
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.canonical.schema import (
    Distance,
    Gate,
    PipelineStep,
    Style,
)

__all__ = [
    "CanonicalEngine",
    "CanonicalError",
    "Distance",
    "Gate",
    "GateResult",
    "PipelineStep",
    "Style",
    "StyleLoader",
    "build_age_clause",
    "run_gate",
]
