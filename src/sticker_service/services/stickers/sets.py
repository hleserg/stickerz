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
)

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
    """
    captions = list(STANDARD_BLOCK)
    if personal:
        captions.extend(personal)
    return captions[:limit]
