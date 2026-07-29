"""Admin CLI: create the first owner, a group, and a seat-limited invite code.

Run once per book club to bootstrap it. Members then self-register by redeeming the
code — you do NOT hand-create each member account.

Usage:
    python scripts/seed_group.py \\
        --email owner@example.com \\
        --password 'a-strong-password' \\
        --display-name 'Club Owner' \\
        --group-name 'The Night Owls' \\
        --code NIGHTOWLS-2026 \\
        --seats 8

--code is optional; omit it to auto-generate a random code (printed on success).
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
from dotenv import load_dotenv

# Allow running as a script (python scripts/seed_group.py) by making the repo importable.
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import auth, db  # noqa: E402
from app.services.invites import generate_code, normalize_and_validate_code  # noqa: E402

load_dotenv()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed a book club, owner, and invite code.")
    parser.add_argument("--email", required=True, help="Owner's email address.")
    parser.add_argument("--password", required=True, help="Owner's password (min 8 chars).")
    parser.add_argument("--display-name", required=True, help="Owner's display name.")
    parser.add_argument("--group-name", required=True, help="Name of the book club / group.")
    parser.add_argument(
        "--group-type",
        default="book_club",
        choices=["book_club", "family"],
        help="Group type (default: book_club).",
    )
    parser.add_argument(
        "--code",
        default=None,
        help="Custom invite code (letters/digits/hyphens, 4–32 chars). Random if omitted.",
    )
    parser.add_argument(
        "--seats",
        required=True,
        type=int,
        help="Maximum redemptions for the invite code (must be >= 1).",
    )
    parser.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="Optional: invite code expires this many days from now.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if args.seats < 1:
        print("Error: --seats must be at least 1.", file=sys.stderr)
        return 2
    if len(args.password) < 8:
        print("Error: --password must be at least 8 characters.", file=sys.stderr)
        return 2

    email = args.email.strip().lower()

    if args.code is None:
        code = generate_code()
        generated = True
    else:
        try:
            code = normalize_and_validate_code(args.code)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        generated = False

    expires_at = None
    if args.expires_days is not None:
        expires_dt = datetime.now(timezone.utc) + timedelta(days=args.expires_days)
        expires_at = db.format_ts(expires_dt)

    await db.init_db()

    try:
        result = await db.create_owner_group_and_invite(
            email=email,
            password_hash=auth.hash_password(args.password),
            display_name=args.display_name.strip(),
            group_name=args.group_name.strip(),
            group_type=args.group_type,
            code=code,
            max_redemptions=args.seats,
            expires_at=expires_at,
        )
    except aiosqlite.IntegrityError as exc:
        # UNIQUE violation on either the email or the code.
        detail = str(exc)
        if "invite_codes.code" in detail:
            print(f"Error: invite code '{code}' already exists. Choose another.", file=sys.stderr)
        elif "users.email" in detail:
            print(f"Error: a user with email '{email}' already exists.", file=sys.stderr)
        else:
            print(f"Error: {detail}", file=sys.stderr)
        return 1

    print("Created book club successfully:")
    print(f"  Owner user id : {result['owner_user_id']}  ({email})")
    print(f"  Group id      : {result['group_id']}  ('{args.group_name.strip()}', {args.group_type})")
    print(f"  Invite code   : {result['code']}  ({args.seats} seat(s)"
          + (f", expires in {args.expires_days} day(s)" if expires_at else "") + ")")
    if generated:
        print("  NOTE: this code was auto-generated — share it with members to let them register.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
