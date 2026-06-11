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
