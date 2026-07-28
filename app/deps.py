"""Reusable authorization dependencies.

require_membership is the pattern later sessions reuse everywhere a route is scoped to
a group: it resolves the current user from the JWT and rejects non-members with 403.
"""
from fastapi import Depends, HTTPException, Request, status

from app import db
from app.auth import get_current_user


def require_membership(path_param: str = "group_id"):
    """Build a dependency that 403s unless the current user belongs to the group.

    The group id is read from the route's path parameter named `path_param`
    (default "group_id"), so the same factory works on any group-scoped route.
    """

    async def _dependency(
        request: Request, user: dict = Depends(get_current_user)
    ) -> dict:
        raw = request.path_params.get(path_param)
        try:
            group_id = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found.")

        if not await db.is_member(user["id"], group_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a member of this group.",
            )
        return {"user": user, "group_id": group_id}

    return _dependency


def require_owner(path_param: str = "group_id"):
    """Build a dependency that 403s unless the current user is the group's OWNER.

    Stricter than require_membership: the user must hold a membership row with role
    'owner' for the group. Used to gate owner-only management endpoints (member
    removal, invite-code lifecycle). Authorization is resolved server-side from the
    JWT + memberships table — never from a role claimed by the client.
    """

    async def _dependency(
        request: Request, user: dict = Depends(get_current_user)
    ) -> dict:
        raw = request.path_params.get(path_param)
        try:
            group_id = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found.")

        if await db.get_role(user["id"], group_id) != db.ROLE_OWNER:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the group owner can do that.",
            )
        return {"user": user, "group_id": group_id}

    return _dependency
