"""Typed domain models for the persistence layer (§3.2).

Two core entities: a **Character** (a saved canonical the user confirmed, which
can spawn many packs) and a **Pack** (one published Telegram sticker set bound
to a character). Plus the in-progress build **Order**, the access **Whitelist**,
and photo-**Consent** records (§15.2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

SubjectType = Literal["adult", "child"]


class Character(BaseModel):
    """A confirmed canonical character; reused across packs (§3.2)."""

    id: int
    owner_id: int
    name: str
    style_id: str
    subject_type: SubjectType
    child_age: int | None
    canonical_path: str
    photo_path: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _check_age(self) -> Character:
        """Age is required for children and forbidden for adults (§B.4)."""
        if self.subject_type == "child":
            if self.child_age is None or not 0 <= self.child_age <= 18:
                raise ValueError("child requires child_age in 0..18")
        elif self.child_age is not None:
            raise ValueError("adult must not carry child_age")
        return self


class Pack(BaseModel):
    """A sticker pack bound to a character; may be a draft or published."""

    id: int
    character_id: int
    owner_id: int
    set_name: str  # machine name, ends with _by_<botusername>
    title: str  # human name (cyrillic/emoji ok)
    created_at: datetime
    published: bool = False  # whether it's live on Telegram

    @property
    def link(self) -> str:
        """Public add link for the set (valid once published)."""
        return f"https://t.me/addstickers/{self.set_name}"


class Sticker(BaseModel):
    """A single sticker placed in a pack."""

    id: int
    pack_id: int
    file_path: str
    emoji: str
    position: int
    created_at: datetime


class Order(BaseModel):
    """The user's in-progress pack-building session ("user collects a pack")."""

    owner_id: int
    state: dict[str, object]
    updated_at: datetime


class WhitelistEntry(BaseModel):
    """An allowed user; user_id is the durable key, username is convenience."""

    user_id: int
    username: str | None
    added_at: datetime


class Application(BaseModel):
    """A test-participation application (§alpha)."""

    user_id: int
    username: str | None
    source: str  # where the user heard about the bot
    created_at: datetime
    status: str  # pending | rejected | approved
