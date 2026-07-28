"""Preference-profile routes and a protected group route that demonstrates the
require_membership authorization pattern.
"""
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app import db
from app.auth import get_current_user
from app.deps import require_membership
from app.models import ProfileResponse, ProfileUpdate

router = APIRouter()

_STATIC = Path(__file__).parent.parent.parent / "static"


@router.get("/profile", include_in_schema=False)
async def profile_page() -> FileResponse:
    """Serve the profile editor page. The page's JS fetches /api/profile and redirects
    to /login on a 401, so no server-side gate is needed here."""
    return FileResponse(str(_STATIC / "profile.html"))


@router.get(
    "/api/profile",
    summary="Get the current user's preference profile",
    response_model=ProfileResponse,
    tags=["profile"],
)
async def get_profile(user: dict = Depends(get_current_user)) -> ProfileResponse:
    """Return the authenticated user's full preference profile (JSON columns parsed)."""
    profile = await db.get_profile(user["id"])
    # get_current_user already confirmed the user exists.
    return ProfileResponse(**profile)


@router.put(
    "/api/profile",
    summary="Update the current user's preference profile",
    response_model=ProfileResponse,
    tags=["profile"],
)
async def put_profile(
    body: ProfileUpdate, user: dict = Depends(get_current_user)
) -> ProfileResponse:
    """Validate and persist the preference profile, then return the stored result."""
    await db.update_profile(
        user_id=user["id"],
        favorite_genres=body.favorite_genres,
        favorite_authors=body.favorite_authors,
        examples=body.examples,
        dislikes=body.dislikes,
        content_preferences=body.content_preferences.model_dump(),
        reading_pace=body.reading_pace,
        preferred_length=body.preferred_length,
    )
    profile = await db.get_profile(user["id"])
    return ProfileResponse(**profile)


@router.get(
    "/api/groups/{group_id}",
    summary="Get a group's details (members only)",
    tags=["groups"],
)
async def get_group(ctx: dict = Depends(require_membership())) -> dict:
    """Return basic group info plus the current user's role in it. Protected by
    require_membership — 403 for non-members.

    The role lets the group page decide whether to render owner controls. This route
    keeps its original authorization behavior; only the response was extended.
    """
    group = await db.get_group(ctx["group_id"])
    role = await db.get_role(ctx["user"]["id"], ctx["group_id"])
    return {"id": group["id"], "name": group["name"], "type": group["type"], "role": role}
