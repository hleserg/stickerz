"""Caption/emotion sets for a sticker sheet (§6.1).

Three blocks: a standard age-neutral reaction block, an optional personal block
(Russian humour, tailored per character), and the cover/title sticker.
"""

from __future__ import annotations

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

# Pack limits: up to 24 stickers, laid out 12 per sheet (3×4), max 2 sheets.
MAX_CAPTIONS = 24
PER_PAGE = 12

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
