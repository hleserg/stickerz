"""Caption/emotion sets for a sticker sheet (§6.1).

Three blocks: a standard age-neutral reaction block, an optional personal block
(Russian humour, tailored per character), and the cover/title sticker.
"""

from __future__ import annotations

import re
from itertools import pairwise

# Standard chat-reaction block — age-neutral (§6.1).
STANDARD_BLOCK: tuple[str, ...] = (
    "Привет!",
    "Класс!",
    "Ха-ха-ха",
    "Грустно",
    "Шок!",
    "Люблю",
    "Задумался",
    "Устал",
    "Окей 😉",
    "Фейспалм",
    "Ты!",
    "Я крутой",
    "Пока!",
)

# Точная строка, уходящая в промпт для каждой стандартной кнопки — и она же
# текст кнопки (полная прозрачность, правило владельца). В кавычках — что
# НАПИСАТЬ; всё без кавычек (включая эмодзи) — что НАРИСОВАТЬ. Кнопки без
# записи здесь уходят в промпт как есть (эмоция = голое слово).
STANDARD_PROMPTS: dict[str, str] = {
    "Привет!": "«Привет!»",
    "Класс!": '👍"Класс!"',
    "Ха-ха-ха": "«Ха-ха-ха»",
    "Окей 😉": '"Ок!" 👌😉',
    "Ты!": "«Ты!»",
    "Я крутой": "😎 Я крутой!",
    "Пока!": "«Пока!»",
}

# Known emoji per standard caption — lets us skip a per-sticker vision call (§9).
STANDARD_EMOJI: dict[str, str] = {
    "Привет!": "👋",
    "Класс!": "👍",
    "Ха-ха-ха": "😂",
    "Грустно": "😢",
    "Шок!": "😱",
    "Люблю": "❤️",
    "Задумался": "🤔",
    "Устал": "😮‍💨",
    "Окей 😉": "😉",
    "Фейспалм": "🤦",
    "Ты!": "👉",
    "Я крутой": "😎",
    "Пока!": "👋",
}

# Pack limit: up to 15 stickers on a single 3-wide sheet (5×3) — one generation.
MAX_CAPTIONS = 15
PER_PAGE = 15

# Example personal block for a child (Russian humour, §6.1).
CHILD_PERSONAL_EXAMPLE: tuple[str, ...] = (
    "Не в садик!",
    "Какао!",
    "Хм...",
    "Серьёзно?",
)


def build_caption_set(
    *,
    personal: list[str] | None = None,
    limit: int = 15,
) -> list[str]:
    """Combine the standard block with an optional personal block.

    Capped at ``limit`` (default 15) so the sheet stays a single generation.
    Personal captions are always kept; the standard block fills the remaining room.
    """
    personal = personal or []
    room = max(0, limit - len(personal))
    captions = list(STANDARD_BLOCK[:room]) + list(personal)
    return captions[:limit]


def selected_captions(std_indices: list[int], custom: list[str]) -> list[str]:
    """Merge chosen standard captions (by index) with custom ones (capped)."""
    std = [STANDARD_BLOCK[i] for i in sorted(set(std_indices)) if 0 <= i < len(STANDARD_BLOCK)]
    return (std + list(custom))[:MAX_CAPTIONS]


# «1. идея» / «2) идея» at the start of a line.
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+(\S.*)$")
# The same marker inline: «1. … 2. … 3. …» in one message line.
_INLINE_MARKER_RE = re.compile(r"(?:^|(?<=\s))(\d{1,2})\s*[.)]\s+")


def _is_sequential(numbers: list[int]) -> bool:
    """1, 2, 3, … — the strongest signal that the numbers really number a list."""
    if not numbers or numbers[0] != 1:
        return False
    return all(b - a == 1 for a, b in pairwise(numbers))


def parse_caption_items(text: str) -> list[str]:
    """Split a numbered-list message into separate captions (owner's rule, 13.06).

    Two layouts count as a list: one item per line («1. идея\\n2. идея») and
    everything inline («1. идея 2. идея 3. идея»). In both, the numbers must
    run 1, 2, 3, … and there must be at least two of them — so a single
    caption that legally contains digits («Гол 1:0!», «С 1 сентября»)
    stays one caption. Unnumbered preamble lines are ignored in list mode.
    Non-list input comes back as a single-item list; empty input as [].
    """
    lined = [
        (int(m.group(1)), m.group(2).strip())
        for line in text.splitlines()
        if (m := _NUMBERED_LINE_RE.match(line))
    ]
    if len(lined) >= 2 and _is_sequential([n for n, _ in lined]):
        return [item for _, item in lined]

    markers = list(_INLINE_MARKER_RE.finditer(text))
    if len(markers) >= 2 and _is_sequential([int(m.group(1)) for m in markers]):
        items = []
        for marker, following in zip(markers, [*markers[1:], None], strict=True):
            end = following.start() if following is not None else len(text)
            item = text[marker.end() : end].strip().strip(",;—-").strip()
            if item:
                items.append(item)
        if len(items) >= 2:
            return items

    return [text.strip()] if text.strip() else []
