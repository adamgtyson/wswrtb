"""Owner/admin group-management routes (Session 2).

Two capabilities:
  * Member roster + removal — any member can view the roster; only the owner can
    remove a (non-owner) member.
  * Invite-code lifecycle — the owner can list, create, and deactivate codes.

Authorization is resolved server-side from the JWT + memberships table via
require_membership / require_owner. Never trust a role claimed by the client.
"""
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from app import db
from app.auth import get_current_user
from app.deps import require_membership, require_owner
from app.models import InviteCodeCreate
from app.services import rate_limit
from app.services.invites import generate_code, normalize_and_validate_code

router = APIRouter()

_STATIC = Path(__file__).parent.parent.parent / "static"


# ----- Page route -----
@router.get("/group", include_in_schema=False)
async def group_page() -> FileResponse:
    """Serve the group-management page. Its JS resolves the group and redirects to
    /login on a 401, so no server-side gate is needed on the page itself."""
    return FileResponse(str(_STATIC / "group.html"))


# ----- Group discovery for the current user -----
@router.get(
    "/api/me/groups",
    summary="List the groups the current user belongs to",
    tags=["groups"],
)
async def my_groups(user: dict = Depends(get_current_user)) -> dict:
    """Return the current user's groups as {id, name, type, role}. Lets the group page
    resolve which group to show without a hardcoded id."""
    return {"groups": await db.list_user_groups(user["id"])}


# ----- Member roster + removal -----
@router.get(
    "/api/groups/{group_id}/members",
    summary="List a group's members (members only)",
    tags=["groups"],
)
async def list_group_members(ctx: dict = Depends(require_membership())) -> dict:
    """Return the roster: display_name, role, and joined_at for each member. Email
    addresses are exposed only to the group owner."""
    group_id = ctx["group_id"]
    requester_is_owner = await db.get_role(ctx["user"]["id"], group_id) == db.ROLE_OWNER

    roster = []
    for m in await db.list_members(group_id):
        row = {
            "user_id": m["user_id"],
            "display_name": m["display_name"],
            "role": m["role"],
            "joined_at": m["joined_at"],
        }
        if requester_is_owner:
            row["email"] = m["email"]
        roster.append(row)

    return {"group_id": group_id, "is_owner": requester_is_owner, "members": roster}


@router.delete(
    "/api/groups/{group_id}/members/{user_id}",
    summary="Remove a member from a group (owner only)",
    tags=["groups"],
)
async def remove_group_member(
    user_id: int, ctx: dict = Depends(require_owner())
) -> dict:
    """Delete a member's membership row (their account and profile are untouched).

    Refuses to remove the group's owner (400). Removing a user who isn't a member
    returns 404, not a 500.
    """
    group_id = ctx["group_id"]
    group = await db.get_group(group_id)
    if group is None:  # pragma: no cover - require_owner already proved membership
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found.")

    if user_id == group["owner_user_id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The group owner cannot be removed.",
        )

    if not await db.remove_membership(user_id, group_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That member was not found in this group.",
        )
    return {"success": True, "removed_user_id": user_id}


# ----- Invite-code lifecycle (owner only) -----
@router.get(
    "/api/groups/{group_id}/invite-codes",
    summary="List a group's invite codes (owner only)",
    tags=["invite-codes"],
)
async def list_group_invite_codes(ctx: dict = Depends(require_owner())) -> dict:
    """Return every code for the group with seat accounting and active state."""
    return {
        "group_id": ctx["group_id"],
        "invite_codes": await db.list_invite_codes(ctx["group_id"]),
    }


@router.post(
    "/api/groups/{group_id}/invite-codes",
    summary="Create a new invite code (owner only)",
    status_code=status.HTTP_201_CREATED,
    tags=["invite-codes"],
)
async def create_group_invite_code(
    body: InviteCodeCreate, ctx: dict = Depends(require_owner())
) -> dict:
    """Create a new active invite code for the group. Uses the shared normalization
    helper for a supplied code (identical rules to seed_group.py); generates a random
    code when none is supplied. Multiple simultaneously-active codes are allowed.

    Rate-limited by owner user id (in addition to the require_owner gate) to cap runaway
    code creation."""
    await rate_limit.check_and_record(
        f"invite_create:{ctx['user']['id']}",
        rate_limit.INVITE_CREATE_LIMIT,
        rate_limit.INVITE_CREATE_WINDOW_SECONDS,
    )
    if body.code is None:
        code = generate_code()
    else:
        try:
            code = normalize_and_validate_code(body.code)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    try:
        created = await db.create_invite_code(
            group_id=ctx["group_id"],
            code=code,
            created_by_user_id=ctx["user"]["id"],
            max_redemptions=body.max_redemptions,
        )
    except aiosqlite.IntegrityError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That invite code already exists. Choose another.",
        )

    return {
        "success": True,
        "id": created["id"],
        "code": created["code"],
        "max_redemptions": body.max_redemptions,
    }


@router.patch(
    "/api/groups/{group_id}/invite-codes/{code_id}/deactivate",
    summary="Deactivate an invite code (owner only)",
    tags=["invite-codes"],
)
async def deactivate_group_invite_code(
    code_id: int, ctx: dict = Depends(require_owner())
) -> dict:
    """Force a code inactive regardless of remaining seats. Deactivating an already-
    inactive code is a no-op success; an unknown code (for this group) returns 404."""
    if not await db.deactivate_invite_code(ctx["group_id"], code_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="That invite code was not found in this group.",
        )
    return {"success": True, "id": code_id, "active": 0}
