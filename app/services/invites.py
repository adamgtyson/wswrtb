"""Invite-code redemption and code-gated account creation.

The seat check-and-decrement and the account creation happen in ONE transaction so
two users racing for the last seat can never both succeed (SQLite serializes writers;
the conditional UPDATE runs first and takes the write lock, so a concurrent redemption
blocks and then re-reads the decremented count). Any failure rolls back the whole unit,
so a rejected registration never consumes a seat.
"""
import re

import aiosqlite

from app import auth, db

# Codes are normalized to uppercase; letters, digits, and hyphens only, 4–32 chars.
_CODE_RE = re.compile(r"^[A-Z0-9-]{4,32}$")


class InviteError(Exception):
    """Raised on any registration failure. Message is intentionally generic — callers
    must not reveal which specific condition failed."""


def normalize_and_validate_code(raw: str) -> str:
    """Normalize a raw code (trim, uppercase) and validate its shape.

    Returns the normalized code. Raises ValueError if it isn't 4–32 chars of
    letters/digits/hyphens. Used by both the seed script and redemption so a code
    always matches regardless of how the user typed it.
    """
    code = (raw or "").strip().upper()
    if not _CODE_RE.match(code):
        raise ValueError(
            "Code must be 4–32 characters, using only letters, digits, and hyphens."
        )
    return code


async def redeem_and_register(
    email: str, password: str, display_name: str, invite_code: str
) -> dict:
    """Atomically redeem an invite code and create the member account.

    Returns {user_id, group_id} on success. Raises InviteError on any failure
    (missing/invalid/expired/full/inactive code, or duplicate email) — always with a
    generic message so no single failing condition is leaked.
    """
    try:
        code = normalize_and_validate_code(invite_code)
    except ValueError:
        raise InviteError("That invite code is invalid or full.")

    password_hash = auth.hash_password(password)
    now = db.utcnow_str()

    async with await db.connect() as conn:
        conn.isolation_level = None  # manual transaction control
        await conn.execute("BEGIN IMMEDIATE")
        try:
            # Consume a seat FIRST. The WHERE clause enforces active / not-expired /
            # seats-remaining atomically; RETURNING hands back the ids we need next.
            # active flips to 0 when this redemption fills the last seat.
            cur = await conn.execute(
                """UPDATE invite_codes
                   SET redemption_count = redemption_count + 1,
                       active = CASE WHEN redemption_count + 1 >= max_redemptions THEN 0 ELSE 1 END
                   WHERE code = ?
                     AND active = 1
                     AND redemption_count < max_redemptions
                     AND (expires_at IS NULL OR expires_at > ?)
                   RETURNING id, group_id""",
                (code, now),
            )
            row = await cur.fetchone()
            if row is None:
                # No row updated: code missing, inactive, expired, or full.
                raise InviteError("That invite code is invalid or full.")
            invite_code_id, group_id = row[0], row[1]

            # Create the user. Duplicate email trips the UNIQUE constraint -> rollback,
            # so the seat we just consumed is released.
            try:
                cur = await conn.execute(
                    "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
                    (email, password_hash, display_name),
                )
            except aiosqlite.IntegrityError:
                raise InviteError("That invite code is invalid or full.")
            user_id = cur.lastrowid

            await conn.execute(
                "INSERT INTO memberships (user_id, group_id, role) VALUES (?, ?, 'member')",
                (user_id, group_id),
            )
            # Audit row; UNIQUE(invite_code_id, user_id) is defense-in-depth against a
            # user redeeming the same code twice.
            await conn.execute(
                "INSERT INTO invite_redemptions (invite_code_id, user_id) VALUES (?, ?)",
                (invite_code_id, user_id),
            )

            await conn.execute("COMMIT")
        except InviteError:
            await conn.execute("ROLLBACK")
            raise
        except Exception:
            await conn.execute("ROLLBACK")
            raise InviteError("That invite code is invalid or full.")

    return {"user_id": user_id, "group_id": group_id}
