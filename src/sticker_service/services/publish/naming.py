"""Machine set-name generation for Telegram sticker sets (§3.3).

The user types a human name (cyrillic/emoji ok) → that becomes the visible
*title*. The machine id is generated silently: ``<translit>_<rand4-6>_by_<bot>``,
matching Telegram's ``[a-z0-9_]`` + ``_by_<botusername>`` requirement. A random
suffix makes collisions vanishingly unlikely; we never pre-check availability
(§3.3) — a single occupied-retry covers the race reactively.
"""

from __future__ import annotations

import random
import re
import string

# Practical lowercase transliteration of Russian for a readable URL slug.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip


def transliterate(text: str) -> str:
    """Lowercase RU→latin slug; keeps [a-z0-9], collapses the rest."""
    out: list[str] = []
    for ch in text.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
    return "".join(out)


def build_set_name(
    title: str,
    bot_username: str,
    *,
    rng: random.Random | None = None,
    max_base: int = 20,
) -> str:
    """Generate a Telegram-valid machine set name ending in ``_by_<bot>``."""
    # Suffix is for collision avoidance, not security/crypto.
    rng = rng or random.Random()  # nosec B311
    base = transliterate(title)[:max_base]
    if not base or not base[0].isalpha():
        base = "s" + base
    length = rng.randint(4, 6)
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(rng.choice(alphabet) for _ in range(length))
    name = f"{base}_{suffix}_by_{bot_username.lower()}"
    # Defensive: enforce the allowed charset.
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):  # pragma: no cover - belt and suspenders
        raise ValueError(f"generated invalid set name: {name!r}")
    return name


# Telegram caps a set's TITLE at 64 chars.
_TITLE_MAX = 64
_PART_RE = re.compile(r"^(?P<base>.*\S)\s+часть\s+(?P<n>\d+)\s*$", re.IGNORECASE)


def next_part_title(title: str) -> str:
    """The continuation title for a FULL pack: «Котя» → «Котя часть 2» → «часть 3»…

    Owner's rule (13.06): when a set hits Telegram's 120-sticker cap, the new
    pack continues the name. A title already ending in «часть N» increments N
    (the base before it is preserved); anything else gets « часть 2». The
    result always fits Telegram's 64-char title cap — the base is trimmed,
    never the part number.
    """
    match = _PART_RE.match(title.strip())
    if match:
        base, part = match.group("base"), int(match.group("n")) + 1
    else:
        base, part = title.strip(), 2
    tail = f" часть {part}"
    return base[: _TITLE_MAX - len(tail)].rstrip() + tail


_LINK_RE = re.compile(
    r"(?:https?://)?t(?:elegram)?\.me/addstickers/(?P<name>[A-Za-z][\w]*)", re.IGNORECASE
)


def parse_set_link(text: str, bot_username: str) -> str | None:
    """Extract OUR bot's set name from a t.me/addstickers link (or a bare name).

    Telegram lets a bot edit only the sets it created, so anything not ending
    in ``_by_<bot>`` returns None — the caller refuses honestly instead of
    failing later, after the user already invested in the flow.
    """
    text = text.strip()
    match = _LINK_RE.search(text)
    name = match.group("name") if match else text
    if not re.fullmatch(r"[A-Za-z][\w]*", name):
        return None
    if not name.lower().endswith(f"_by_{bot_username.lower()}"):
        return None
    return name


def sticker_set_link(set_name: str) -> str:
    """Public add link for a published set."""
    return f"https://t.me/addstickers/{set_name}"
