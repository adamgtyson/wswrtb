"""Pydantic request/response schemas. Server-side validation lives here — never trust
the client. These schemas define the preference data model that a future conversational
(Claude) discovery layer must augment, not fork.
"""
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Basic email shape check — avoids pulling in the email-validator dependency while still
# rejecting obviously malformed addresses server-side.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Caps to keep stored profile data bounded (defense against oversized payloads).
_MAX_LIST_ITEMS = 50
_MAX_ITEM_LEN = 200
_CONTENT_LEVELS = ("none", "mild", "moderate", "graphic")


def _clean_string_list(value: list) -> list[str]:
    """Strip, drop empties, enforce per-item length and list-length caps."""
    if not isinstance(value, list):
        raise ValueError("must be a list")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("list items must be strings")
        item = item.strip()
        if not item:
            continue
        if len(item) > _MAX_ITEM_LEN:
            raise ValueError(f"each item must be at most {_MAX_ITEM_LEN} characters")
        cleaned.append(item)
    if len(cleaned) > _MAX_LIST_ITEMS:
        raise ValueError(f"at most {_MAX_LIST_ITEMS} items allowed")
    return cleaned


# ---------------------------------------------------------------------------
# Auth / registration
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    """Code-gated registration payload. All four fields are required."""

    email: str = Field(..., max_length=254)
    password: str = Field(..., min_length=8, max_length=200)
    display_name: str = Field(..., min_length=1, max_length=80)
    invite_code: str = Field(..., min_length=1, max_length=64)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v

    @field_validator("display_name")
    @classmethod
    def _clean_display_name(cls, v: str) -> str:
        return v.strip()


class LoginRequest(BaseModel):
    """Login payload."""

    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=200)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return v.strip().lower()


# ---------------------------------------------------------------------------
# Preference profile
# ---------------------------------------------------------------------------
class ContentPreferences(BaseModel):
    """Content comfort limits. Levels are ordinal: none < mild < moderate < graphic."""

    max_violence: Literal["none", "mild", "moderate", "graphic"] = "moderate"
    max_language: Literal["none", "mild", "moderate", "graphic"] = "moderate"
    romance_ok: bool = True
    explicit_ok: bool = False


class ProfileUpdate(BaseModel):
    """Structured preference-profile submission. Every field is validated and bounded."""

    favorite_genres: list[str] = Field(default_factory=list)
    favorite_authors: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    content_preferences: ContentPreferences = Field(default_factory=ContentPreferences)
    reading_pace: Optional[Literal["fast", "slow"]] = None
    preferred_length: Optional[Literal["short", "medium", "long"]] = None

    @field_validator("favorite_genres", "favorite_authors", "examples", "dislikes")
    @classmethod
    def _validate_lists(cls, v: list) -> list[str]:
        return _clean_string_list(v)


class ProfileResponse(BaseModel):
    """Full profile returned to the client (includes read-only identity fields)."""

    email: str
    display_name: str
    favorite_genres: list[str]
    favorite_authors: list[str]
    examples: list[str]
    dislikes: list[str]
    content_preferences: ContentPreferences
    reading_pace: Optional[str] = None
    preferred_length: Optional[str] = None
