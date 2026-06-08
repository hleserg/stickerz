"""Per-action cost in credits and pack formatting (alpha economics, §12).

Credits are stored in **half-packs** (1 pack = 2 credits) so a half-pack action
is a whole integer. Action costs:

- new full pack (canonical + sheet) ...... 1 pack
- add stickers to an existing canonical ... 0.5 pack
- redraw an existing canonical ............ 1 pack
"""

from __future__ import annotations

from sticker_service.db.repository import CREDITS_PER_PACK

COST_NEW_PACK = CREDITS_PER_PACK  # 1 pack
COST_ADD_STICKERS = CREDITS_PER_PACK // 2  # 0.5 pack
COST_REDRAW = CREDITS_PER_PACK  # 1 pack

# Pack-building modes (see handlers.flow) → their credit cost.
_MODE_COST = {
    "fresh": COST_NEW_PACK,  # brand-new character + pack
    "reuse": COST_ADD_STICKERS,  # new pack from a saved canonical
    "extend": COST_ADD_STICKERS,  # append to an existing published pack
}


def cost_for_mode(mode: str) -> int:
    """Credit cost (half-packs) for a pack-building ``mode``; defaults to a new pack."""
    return _MODE_COST.get(mode, COST_NEW_PACK)


def format_packs(credits: int) -> str:
    """Render a credit balance as packs: 6→'3', 5→'2.5', 1→'0.5', 0→'0'."""
    packs = credits / CREDITS_PER_PACK
    return f"{packs:.1f}".rstrip("0").rstrip(".")
