"""Tests for the data-driven style engine: schema validation + loader cache."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sticker_service.config import get_settings
from sticker_service.services.canonical import Gate, Style, StyleLoader

_VALID = """
schema_version: 1
style_id: {sid}
display_name: "Demo"
enabled: {enabled}
distance: near
pipeline:
  - step: 1
    prompt: "photo to portrait {{age_clause}}"
    refs: [photo]
  - step: 2
    prompt: "to final, hold face"
    refs: [prev]
sticker_style_suffix: "demo suffix"
"""


def _write(dir_: Path, sid: str, *, enabled: bool = True, body: str | None = None) -> Path:
    path = dir_ / f"{sid}.yaml"
    path.write_text(body or _VALID.format(sid=sid, enabled=str(enabled).lower()), encoding="utf-8")
    return path


# --- schema -----------------------------------------------------------------


def test_step_gate_defaults_to_vision_judge() -> None:
    style = Style.model_validate(
        {
            "schema_version": 1,
            "style_id": "x",
            "display_name": "X",
            "distance": "near",
            "pipeline": [{"step": 1, "prompt": "a", "refs": ["photo"]}],
        }
    )
    assert style.pipeline[0].gate is Gate.VISION_JUDGE


@pytest.mark.parametrize(
    "bad",
    [
        {"refs": ["bogus"]},  # invalid ref
        {"refs": []},  # empty refs
        {"prompt": "hi {unknown}"},  # unknown placeholder
    ],
)
def test_invalid_step_rejected(bad: dict[str, object]) -> None:
    step = {"step": 1, "prompt": "ok", "refs": ["photo"], **bad}
    with pytest.raises(ValidationError):
        Style.model_validate(
            {
                "schema_version": 1,
                "style_id": "x",
                "display_name": "X",
                "distance": "near",
                "pipeline": [step],
            }
        )


def test_step_ref_step_n_allowed() -> None:
    style = Style.model_validate(
        {
            "schema_version": 1,
            "style_id": "x",
            "display_name": "X",
            "distance": "far",
            "pipeline": [
                {"step": 1, "prompt": "a", "refs": ["photo"]},
                {"step": 2, "prompt": "b", "refs": ["prev", "step_1"]},
            ],
        }
    )
    assert style.pipeline[1].refs == ["prev", "step_1"]


def test_non_monotonic_steps_rejected() -> None:
    with pytest.raises(ValidationError, match="monotonic"):
        Style.model_validate(
            {
                "schema_version": 1,
                "style_id": "x",
                "display_name": "X",
                "distance": "near",
                "pipeline": [
                    {"step": 1, "prompt": "a", "refs": ["photo"]},
                    {"step": 3, "prompt": "b", "refs": ["prev"]},
                ],
            }
        )


def test_unknown_placeholder_in_suffix_rejected() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        Style.model_validate(
            {
                "schema_version": 1,
                "style_id": "x",
                "display_name": "X",
                "distance": "near",
                "pipeline": [{"step": 1, "prompt": "a", "refs": ["photo"]}],
                "sticker_style_suffix": "bad {nope}",
            }
        )


# --- loader -----------------------------------------------------------------


def test_loader_caches_valid_and_filters_menu(tmp_path: Path) -> None:
    _write(tmp_path, "good")
    _write(tmp_path, "hidden", enabled=False)
    _write(tmp_path, "broken", body="schema_version: 1\nstyle_id: broken\n")  # missing fields

    loader = StyleLoader(tmp_path)
    styles = loader.load()

    assert set(styles) == {"good", "hidden"}  # broken skipped, both valid cached
    assert loader.menu() == [("good", "Demo")]  # only enabled in the menu
    assert loader.get("broken") is None


def test_loader_rejects_id_filename_mismatch(tmp_path: Path) -> None:
    _write(tmp_path, "mismatch", body=_VALID.format(sid="other", enabled="true"))
    loader = StyleLoader(tmp_path)
    assert loader.load() == {}


def test_loader_missing_dir_is_empty(tmp_path: Path) -> None:
    loader = StyleLoader(tmp_path / "nope")
    assert loader.load() == {}
    assert loader.menu() == []


def test_loader_reload_picks_up_changes(tmp_path: Path) -> None:
    loader = StyleLoader(tmp_path)
    assert loader.load() == {}
    _write(tmp_path, "good")
    assert set(loader.reload()) == {"good"}


def test_real_styles_load() -> None:
    loader = StyleLoader(get_settings().styles_dir)
    watercolor = loader.get("watercolor")
    assert watercolor is not None
    assert watercolor.display_name == "Акварель"
    assert len(watercolor.pipeline) == 3
    # Step 3 carries the gaze pre-check.
    assert watercolor.pipeline[2].skip_if_yes is not None

    soft_anime = loader.get("soft_anime")
    assert soft_anime is not None
    assert len(soft_anime.pipeline) == 3

    menu_ids = {sid for sid, _ in loader.menu()}
    assert {"watercolor", "soft_anime"} <= menu_ids

    # Minecraft ships as an experimental style: hidden from the default menu,
    # visible only on the experimental shelf; step 2 is the Minecraft style step.
    minecraft = loader.get("minecraft")
    assert minecraft is not None
    assert minecraft.experimental is True
    assert minecraft.pipeline[1].prompt == "А теперь нарисуй героя в стиле Minecraft."
    assert "minecraft" not in menu_ids
    assert "minecraft" in {sid for sid, _ in loader.menu(experimental=True)}


_EXP = """
schema_version: 1
style_id: {sid}
display_name: "Exp"
enabled: true
experimental: true
distance: far
pipeline:
  - step: 1
    prompt: "photo {{clean_bg}}"
    refs: [photo]
  - step: 2
    prompt: "to final"
    refs: [prev]
sticker_style_suffix: ""
"""


def test_experimental_defaults_false_and_menu_splits_tiers(tmp_path: Path) -> None:
    _write(tmp_path, "polished")  # no experimental key → polished tier
    (tmp_path / "exp.yaml").write_text(_EXP.format(sid="exp"), encoding="utf-8")
    loader = StyleLoader(tmp_path)
    loader.load()

    polished = loader.get("polished")
    experimental = loader.get("exp")
    assert polished is not None and polished.experimental is False
    assert experimental is not None and experimental.experimental is True
    assert loader.menu() == [("polished", "Demo")]  # default tier only
    assert loader.menu(experimental=True) == [("exp", "Exp")]
    assert loader.has_experimental() is True


def test_has_experimental_false_without_any(tmp_path: Path) -> None:
    _write(tmp_path, "polished")
    loader = StyleLoader(tmp_path)
    loader.load()
    assert loader.has_experimental() is False
    assert loader.menu(experimental=True) == []
