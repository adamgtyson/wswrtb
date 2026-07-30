"""Pydantic request/response schemas. Server-side validation lives here — never trust
the client. These schemas define the preference data model that a future conversational
(Claude) discovery layer must augment, not fork.
"""
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.services.claude_service import MAX_PROMPT_CHARS

# Basic email shape check — avoids pulling in the email-validator dependency while still
# rejecting obviously malformed addresses server-side.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Caps to keep stored profile data bounded (defense against oversized payloads).
_MAX_LIST_ITEMS = 50
_MAX_ITEM_LEN = 200
_CONTENT_LEVELS = ("none", "mild", "moderate", "graphic")

# Invite-code seat bounds for owner-created codes.
_MIN_SEATS = 1
_MAX_SEATS = 200

# Recommendation request bounds (Session 3). The prompt length cap is shared with the
# Claude service so the validation limit and the request-shape limit can't drift apart.
_MIN_SELECTED_MEMBERS = 1
_MAX_SELECTED_MEMBERS = 50  # matches the largest group a seat-limited invite can build
_MAX_BOOK_FIELD_LEN = 300
_MAX_REASON_LEN = 1000
_MIN_BOOK_YEAR = 1
_MAX_BOOK_YEAR = 2100


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


# ---------------------------------------------------------------------------
# Invite-code management (Session 2)
# ---------------------------------------------------------------------------
class InviteCodeCreate(BaseModel):
    """Owner-supplied parameters for a new invite code.

    `code` is optional (a random code is generated when omitted). A supplied code's
    shape (4–32 chars, letters/digits/hyphens, normalized) is validated server-side by
    the shared invites helper — the same rules seed_group.py uses — so only a light
    length bound is enforced here. `max_redemptions` is the seat count.
    """

    code: Optional[str] = Field(default=None, max_length=64)
    max_redemptions: int = Field(..., ge=_MIN_SEATS, le=_MAX_SEATS)

    @field_validator("code")
    @classmethod
    def _blank_to_none(cls, v: Optional[str]) -> Optional[str]:
        """Treat a blank/whitespace code as omitted (-> random)."""
        if v is None:
            return None
        v = v.strip()
        return v or None


# ---------------------------------------------------------------------------
# Recommendations (Session 3)
# ---------------------------------------------------------------------------
class RecommendRequest(BaseModel):
    """A member's recommendation request: free-text prompt + who's reading.

    `member_user_ids` is validated against the group's actual membership in the route —
    a caller cannot pull another group's profiles into their prompt by guessing ids.
    """

    prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT_CHARS)
    member_user_ids: list[int] = Field(
        ..., min_length=_MIN_SELECTED_MEMBERS, max_length=_MAX_SELECTED_MEMBERS
    )

    @field_validator("prompt")
    @classmethod
    def _clean_prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("prompt must not be blank")
        return v

    @field_validator("member_user_ids")
    @classmethod
    def _dedupe_ids(cls, v: list[int]) -> list[int]:
        """Drop duplicate ids while preserving order; reject empties after dedup."""
        seen: set[int] = set()
        cleaned: list[int] = []
        for user_id in v:
            if user_id not in seen:
                seen.add(user_id)
                cleaned.append(user_id)
        if not cleaned:
            raise ValueError("select at least one member")
        return cleaned


class Recommendation(BaseModel):
    """One book Claude returned. Validates the model's output — never trusted as-is.

    Session 4 adds a `google_books_id` here once there is a way to verify it against the
    Google Books API; asking for an unverifiable id now would be worse than not asking.
    """

    title: str = Field(..., min_length=1, max_length=_MAX_BOOK_FIELD_LEN)
    author: str = Field(..., min_length=1, max_length=_MAX_BOOK_FIELD_LEN)
    year: Optional[int] = Field(default=None, ge=_MIN_BOOK_YEAR, le=_MAX_BOOK_YEAR)
    reason: str = Field(..., min_length=1, max_length=_MAX_REASON_LEN)

    @field_validator("title", "author", "reason")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        return v.strip()

    @field_validator("year", mode="before")
    @classmethod
    def _coerce_year(cls, v: Any) -> Optional[int]:
        """Accept a year given as a number, a numeric string, or null/unknown.

        LLMs return years inconsistently ("1965", 1965, "unknown", ""). Anything that
        isn't a plain integer year becomes None rather than failing the whole response.
        """
        if v is None or isinstance(v, int):
            return v
        if isinstance(v, str):
            v = v.strip()
            return int(v) if v.isdigit() else None
        return None


class RecommendationResponse(BaseModel):
    """Response payload for a recommendation request."""

    group_id: int
    count: int
    recommendations: list[Recommendation]
