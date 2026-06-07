"""Pydantic schema for data-driven style plugins (§5.1).

A style is a self-contained YAML file. The engine is fully data-driven: one
generic executor walks ``pipeline`` steps — there is no ``if style == "..."``
anywhere (invariant §B.4). This module defines and validates the file shape:
the ``refs`` enum, the per-step ``gate``, and prompt placeholders.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# Placeholders the engine knows how to resolve at run time (§5.1.2). A prompt
# referencing anything else fails validation, so the style is skipped.
KNOWN_PLACEHOLDERS = frozenset({"age_clause"})

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_STEP_REF_RE = re.compile(r"^step_(\d+)$")
_SIMPLE_REFS = frozenset({"photo", "prev", "canonical"})


def _unknown_placeholders(text: str) -> set[str]:
    return {name for name in _PLACEHOLDER_RE.findall(text) if name not in KNOWN_PLACEHOLDERS}


class Distance(StrEnum):
    """Distance photo → target style; informs the step count (§4.2)."""

    FAR = "far"
    MEDIUM = "medium"
    NEAR = "near"


class Gate(StrEnum):
    """Face-geometry gate run between steps (§4.3). Default is vision_judge."""

    VISION_JUDGE = "vision_judge"
    FACE_GEOMETRY = "face_geometry"
    NONE = "none"


class PipelineStep(BaseModel):
    """One step of the sliding-anchor pipeline."""

    model_config = ConfigDict(extra="forbid")

    step: int
    prompt: str
    refs: list[str]
    gate: Gate = Gate.VISION_JUDGE

    @field_validator("refs")
    @classmethod
    def _validate_refs(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("step must reference at least one image")
        for ref in value:
            if ref in _SIMPLE_REFS or _STEP_REF_RE.match(ref):
                continue
            raise ValueError(f"invalid ref '{ref}' (allowed: photo, prev, canonical, step_N)")
        return value

    @field_validator("prompt")
    @classmethod
    def _validate_prompt(cls, value: str) -> str:
        unknown = _unknown_placeholders(value)
        if unknown:
            raise ValueError(f"unknown placeholder(s) in prompt: {sorted(unknown)}")
        return value


class Style(BaseModel):
    """A validated style plugin loaded from ``styles/<id>.yaml``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    style_id: str
    display_name: str
    enabled: bool = True
    distance: Distance
    pipeline: list[PipelineStep]
    sticker_style_suffix: str = ""

    @field_validator("sticker_style_suffix")
    @classmethod
    def _validate_suffix(cls, value: str) -> str:
        unknown = _unknown_placeholders(value)
        if unknown:
            raise ValueError(f"unknown placeholder(s) in suffix: {sorted(unknown)}")
        return value

    @model_validator(mode="after")
    def _validate_pipeline(self) -> Style:
        if not self.pipeline:
            raise ValueError("pipeline must have at least one step")
        expected = list(range(1, len(self.pipeline) + 1))
        actual = [s.step for s in self.pipeline]
        if actual != expected:
            raise ValueError(f"steps must be monotonic 1..N, got {actual}")
        return self
