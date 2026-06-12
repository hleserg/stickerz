"""Caption fidelity gate: drawn sheet texts must match the ordered ideas.

The sheet prompt's quote rule (unquoted = draw, quoted = write) is soft — under
load the model duplicates captions, drops them or strands them on neighbour
tiles (live: 6 of 15 tiles defective on 2026-06-12), and three prompt rewrites
in four days did not hold. So the contract is enforced by code, in line with
the project rule that every model output is gated: one cheap vision call lists
the inscriptions actually drawn on the sheet, and a missing or duplicated
caption fails the page into the free-retry path. Extra unexpected texts are
reported but never fatal on their own: vision OCR over stylised Cyrillic is
too noisy to spend a paid regeneration on it.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sticker_service.services.stickers.generate import prompt_idea

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sticker_service.services.models.base import ImageModel

logger = logging.getLogger(__name__)

# «…», "…", „…“ — the first quoted fragment of an idea is its mandatory text.
# Single quotes ('') are accepted by the prompt but skipped here on purpose:
# apostrophes would misfire, and a missed expectation only softens the gate.
_QUOTED_RE = re.compile(r'[«"„]([^»"“„]+)[»"“]')

_NON_WORD_RE = re.compile(r"[^\w@]+")

_NO_TEXT_SENTINEL = "НЕТ"

_QUESTION = (
    "Перечисли все надписи (текст), нарисованные на этом листе стикеров. "
    "Каждую надпись выведи на отдельной строке ровно так, как она написана, "
    f"без нумерации и комментариев. Если надписей нет — ответь ровно: {_NO_TEXT_SENTINEL}"
)


def expected_caption(item: str) -> str | None:
    """The inscription the sheet MUST carry for one idea, or None (no text).

    Mirrors the prompt's quote rule exactly, including the standard-button
    mapping: the first quoted fragment is written, anything unquoted is drawn.
    """
    match = _QUOTED_RE.search(prompt_idea(item))
    if match:
        return match.group(1).strip() or None
    return None


def expected_captions(captions: Sequence[str]) -> list[str]:
    """Mandatory inscriptions for a page of ideas, in order, omitting textless ones."""
    return [text for item in captions if (text := expected_caption(item)) is not None]


def _norm(text: str) -> str:
    return _NON_WORD_RE.sub(" ", text.casefold().replace("ё", "е")).strip()


def _matches(drawn: str, expected: str) -> bool:
    a, b = _norm(drawn), _norm(expected)
    if not a or not b:
        return False
    if a == b:
        return True
    # Containment covers punctuation/truncation drift; ratio covers OCR noise.
    if min(len(a), len(b)) >= 3 and (a in b or b in a):
        return True
    # Fuzzy matching only helps long captions: on short words a one-letter
    # delta still clears the threshold («шок» vs «ок» is exactly 0.8 — the
    # live 2026-06-12 false rejection), so short ones must match exactly.
    if min(len(a), len(b)) < 4:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


@dataclass(frozen=True)
class CaptionVerdict:
    """Outcome of comparing drawn inscriptions against the ordered ideas."""

    ok: bool
    reason: str
    missing: tuple[str, ...] = ()
    duplicated: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()


def judge_captions(drawn: Sequence[str], expected: Sequence[str]) -> CaptionVerdict:
    """Pass iff every expected inscription is drawn exactly once.

    Extra texts ride along in ``extra`` for the owner's alert but never fail
    the verdict by themselves (OCR noise must not burn a paid regeneration).
    """
    remaining = list(expected)
    satisfied: list[str] = []
    duplicated: list[str] = []
    extra: list[str] = []
    lines = [line for line in drawn if _norm(line)]
    # Exact matches claim their captions first, so a fuzzy lookalike drawn
    # nearby (a stray «Шок!» next to the real «Ок!») can never steal a slot
    # and turn the genuine inscription into a false duplicate.
    fuzzy_lines: list[str] = []
    for line in lines:
        hit = next((e for e in remaining if _norm(line) == _norm(e)), None)
        if hit is not None:
            remaining.remove(hit)
            satisfied.append(hit)
        else:
            fuzzy_lines.append(line)
    for line in fuzzy_lines:
        hit = next((e for e in remaining if _matches(line, e)), None)
        if hit is not None:
            remaining.remove(hit)
            satisfied.append(hit)
            continue
        again = next((e for e in satisfied if _matches(line, e)), None)
        if again is not None:
            duplicated.append(again)
            continue
        if len(_norm(line)) >= 4:
            extra.append(line)
    problems: list[str] = []
    if remaining:
        problems.append("пропали: " + ", ".join(f"«{t}»" for t in remaining))
    if duplicated:
        problems.append("задублились: " + ", ".join(f"«{t}»" for t in duplicated))
    ok = not problems
    if extra:
        problems.append("лишние: " + ", ".join(f"«{t}»" for t in extra))
    return CaptionVerdict(
        ok=ok,
        reason="; ".join(problems) if problems else "ok",
        missing=tuple(remaining),
        duplicated=tuple(duplicated),
        extra=tuple(extra),
    )


_SCENE_OK = ("ок", "ok", "нет")


def _scene_question(ideas: Sequence[str]) -> str:
    numbered = "\n".join(f"{i}. {prompt_idea(c)}" for i, c in enumerate(ideas, 1))
    return (
        "Вот идеи листа стикеров по порядку (слева направо, сверху вниз):\n"
        f"{numbered}\n"
        "Сравни лист с идеями. Назови номера идей, чья сцена на листе не нарисована "
        "или перемешана с соседней (предмет/жест уехал на чужой стикер), и кратко "
        "почему. Если всё соответствует — ответь ровно: ОК"
    )


async def review_scenes(model: ImageModel, sheet: bytes, ideas: Sequence[str]) -> str | None:
    """Scene-mismatch complaint for the owner's observer alert, or None (fine/unavailable).

    Observe-only by design: judging «does the drawing match the idea» is
    subjective, so this never gets rejection power until its precision is
    proven on real alerts. Fails open like the caption gate.
    """
    try:
        answer = await model.ask(sheet, _scene_question(ideas))
    except Exception:
        logger.warning("scene observer: vision call failed; skipping", exc_info=True)
        return None
    answer = (answer or "").strip()
    if (
        not answer
        or answer.casefold().rstrip(".!") in _SCENE_OK
        or answer.casefold().startswith("ок")
    ):
        return None
    return answer


async def read_sheet_texts(model: ImageModel, sheet: bytes) -> list[str] | None:
    """Inscriptions the vision model sees on the sheet; ``None`` = unavailable.

    Fails OPEN (None) on any vision trouble: this gate exists to save paid
    retries on bad sheets — it must never burn money or block shipping
    because the checking call itself was flaky.
    """
    try:
        answer = await model.ask(sheet, _QUESTION)
    except Exception:
        logger.warning("caption gate: vision call failed; skipping the check", exc_info=True)
        return None
    answer = (answer or "").strip()
    if not answer:
        return None
    if answer.casefold().startswith(_NO_TEXT_SENTINEL.casefold()):
        return []
    lines = (line.strip(" \t•–—-*·") for line in answer.splitlines())
    return [line for line in lines if line]
