"""SQLite schema initialization and async connection/query helpers.

All schema is CREATE TABLE IF NOT EXISTS so later sessions need no migration.
Every connection enables foreign-key enforcement. All SQL is parameterized —
never interpolate values into a query string.
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Default DB file sits in the project root; override with WSWRTB_DB_PATH (used by tests).
_DEFAULT_DB = Path(__file__).parent.parent / "wswrtb.db"
DB_PATH = Path(os.environ["WSWRTB_DB_PATH"]) if os.environ.get("WSWRTB_DB_PATH") else _DEFAULT_DB

# UTC timestamp format used for invite expiry. ISO-ish and lexicographically sortable,
# so string comparison in SQL (expires_at > ?) behaves like chronological comparison.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Schema — active tables (Session 1) followed by forward-declared tables.
# ---------------------------------------------------------------------------
_TABLES = [
    """CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        display_name TEXT NOT NULL,
        email_verified INTEGER NOT NULL DEFAULT 0,
        plan TEXT NOT NULL DEFAULT 'free',
        favorite_genres TEXT NOT NULL DEFAULT '[]',
        favorite_authors TEXT NOT NULL DEFAULT '[]',
        examples TEXT NOT NULL DEFAULT '[]',
        dislikes TEXT NOT NULL DEFAULT '[]',
        content_preferences TEXT NOT NULL DEFAULT '{}',
        reading_pace TEXT,
        preferred_length TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'book_club',
        owner_user_id INTEGER NOT NULL REFERENCES users(id),
        plan TEXT NOT NULL DEFAULT 'free',
        content_ceiling TEXT NOT NULL DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS memberships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id),
        group_id INTEGER NOT NULL REFERENCES groups(id),
        role TEXT NOT NULL DEFAULT 'member',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, group_id)
    )""",
    """CREATE TABLE IF NOT EXISTS invite_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL REFERENCES groups(id),
        code TEXT UNIQUE NOT NULL,
        created_by_user_id INTEGER REFERENCES users(id),
        max_redemptions INTEGER NOT NULL,
        redemption_count INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        expires_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS invite_redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        invite_code_id INTEGER NOT NULL REFERENCES invite_codes(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        redeemed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(invite_code_id, user_id)
    )""",
    # ----- Forward-declared for later sessions (created now, unused this session) -----
    """CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
        google_books_id TEXT, title TEXT NOT NULL, author TEXT,
        rating INTEGER NOT NULL CHECK(rating IN (-1,1)),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(user_id, title)
    )""",
    """CREATE TABLE IF NOT EXISTS reading_list (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
        google_books_id TEXT, title TEXT NOT NULL, author TEXT, data_json TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS voting_rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id),
        created_by_user_id INTEGER NOT NULL REFERENCES users(id), method TEXT NOT NULL,
        candidates_json TEXT NOT NULL, candidate_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open', prompt TEXT, watching_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, closed_at TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS ballots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL REFERENCES voting_rounds(id),
        user_id INTEGER NOT NULL REFERENCES users(id), book_key TEXT NOT NULL, rank INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(round_id, user_id, book_key)
    )""",
    """CREATE TABLE IF NOT EXISTS ai_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
        group_id INTEGER REFERENCES groups(id), model TEXT NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, est_cost_usd REAL NOT NULL,
        endpoint TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS recent_searches (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id),
        user_id INTEGER NOT NULL REFERENCES users(id), prompt TEXT NOT NULL,
        result_count INTEGER NOT NULL, watching TEXT NOT NULL, results_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS api_cache (
        cache_key TEXT PRIMARY KEY, response_json TEXT NOT NULL,
        cached_at TIMESTAMP NOT NULL, expires_at TIMESTAMP NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES groups(id),
        user_id INTEGER NOT NULL REFERENCES users(id), title TEXT NOT NULL, author TEXT,
        conversation_context TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]

# Columns that store JSON. Parsed on read, dumped on write.
_JSON_LIST_COLUMNS = ("favorite_genres", "favorite_authors", "examples", "dislikes")
_JSON_OBJ_COLUMNS = ("content_preferences",)


def utcnow_str() -> str:
    """Return the current UTC time formatted for storage/comparison in SQLite."""
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def format_ts(dt: datetime) -> str:
    """Format a datetime (assumed/normalized to UTC) for storage/comparison."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime(_TS_FORMAT)


@asynccontextmanager
async def connect(isolation_level: str | None = ""):
    """Async context manager yielding a connection with foreign keys enforced.

    Usage: `async with connect() as db: ...`. The connection is closed on exit.

    `isolation_level` is passed to the underlying driver at creation time so it is
    set on the correct worker thread. Pass ``None`` for manual transaction control
    (explicit BEGIN/COMMIT/ROLLBACK); the default "" keeps sqlite3's legacy
    autocommit-with-implicit-transactions behavior used by the simple query helpers.
    """
    db = await aiosqlite.connect(DB_PATH, isolation_level=isolation_level)
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create every table if it does not already exist. Raises RuntimeError on failure."""
    try:
        async with connect() as db:
            for ddl in _TABLES:
                await db.execute(ddl)
            await db.commit()
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Database initialization failed: {exc}") from exc
    logger.info("Database initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# User / auth lookups
# ---------------------------------------------------------------------------
async def get_user_by_email(email: str) -> dict | None:
    """Return {id, email, display_name, password_hash} for an email, or None."""
    async with connect() as db:
        async with db.execute(
            "SELECT id, email, display_name, password_hash FROM users WHERE email = ?",
            (email,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "email": row[1], "display_name": row[2], "password_hash": row[3]}


async def get_user_by_id(user_id: int) -> dict | None:
    """Return {id, email, display_name} for a user id, or None."""
    async with connect() as db:
        async with db.execute(
            "SELECT id, email, display_name FROM users WHERE id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "email": row[1], "display_name": row[2]}


# ---------------------------------------------------------------------------
# Profile read/write — JSON columns parsed/serialized here.
# ---------------------------------------------------------------------------
async def get_profile(user_id: int) -> dict | None:
    """Return the full preference profile for a user (JSON columns parsed), or None."""
    async with connect() as db:
        async with db.execute(
            """SELECT email, display_name, favorite_genres, favorite_authors, examples,
                      dislikes, content_preferences, reading_pace, preferred_length
               FROM users WHERE id = ?""",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "email": row[0],
        "display_name": row[1],
        "favorite_genres": json.loads(row[2]),
        "favorite_authors": json.loads(row[3]),
        "examples": json.loads(row[4]),
        "dislikes": json.loads(row[5]),
        "content_preferences": json.loads(row[6]),
        "reading_pace": row[7],
        "preferred_length": row[8],
    }


async def update_profile(
    user_id: int,
    favorite_genres: list,
    favorite_authors: list,
    examples: list,
    dislikes: list,
    content_preferences: dict,
    reading_pace: str | None,
    preferred_length: str | None,
) -> None:
    """Persist a validated preference profile to the user's row (JSON as strings)."""
    async with connect() as db:
        await db.execute(
            """UPDATE users SET
                 favorite_genres = ?, favorite_authors = ?, examples = ?, dislikes = ?,
                 content_preferences = ?, reading_pace = ?, preferred_length = ?
               WHERE id = ?""",
            (
                json.dumps(favorite_genres),
                json.dumps(favorite_authors),
                json.dumps(examples),
                json.dumps(dislikes),
                json.dumps(content_preferences),
                reading_pace,
                preferred_length,
                user_id,
            ),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Membership / group
# ---------------------------------------------------------------------------
async def is_member(user_id: int, group_id: int) -> bool:
    """Return True if the user has a membership row for the group."""
    async with connect() as db:
        async with db.execute(
            "SELECT 1 FROM memberships WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ) as cur:
            return await cur.fetchone() is not None


async def get_group(group_id: int) -> dict | None:
    """Return {id, name, type, owner_user_id} for a group, or None."""
    async with connect() as db:
        async with db.execute(
            "SELECT id, name, type, owner_user_id FROM groups WHERE id = ?",
            (group_id,),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "type": row[2], "owner_user_id": row[3]}


# ---------------------------------------------------------------------------
# Admin bootstrap — used by scripts/seed_group.py and the test suite so both
# exercise one code path.
# ---------------------------------------------------------------------------
async def create_owner_group_and_invite(
    *,
    email: str,
    password_hash: str,
    display_name: str,
    group_name: str,
    group_type: str,
    code: str,
    max_redemptions: int,
    expires_at: str | None = None,
) -> dict:
    """Create an owner user, a group, the owner membership, and one invite code.

    Runs in a single transaction. Raises aiosqlite.IntegrityError if the email or
    code already exists (UNIQUE violation). Returns the created ids.
    """
    async with connect(isolation_level=None) as db:  # manual transaction control
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)",
                (email, password_hash, display_name),
            )
            owner_id = cur.lastrowid
            cur = await db.execute(
                "INSERT INTO groups (name, type, owner_user_id) VALUES (?, ?, ?)",
                (group_name, group_type, owner_id),
            )
            group_id = cur.lastrowid
            await db.execute(
                "INSERT INTO memberships (user_id, group_id, role) VALUES (?, ?, 'owner')",
                (owner_id, group_id),
            )
            cur = await db.execute(
                """INSERT INTO invite_codes (group_id, code, created_by_user_id, max_redemptions, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (group_id, code, owner_id, max_redemptions, expires_at),
            )
            invite_code_id = cur.lastrowid
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise
    return {
        "owner_user_id": owner_id,
        "group_id": group_id,
        "invite_code_id": invite_code_id,
        "code": code,
    }
