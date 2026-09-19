"""SQLite schema initialization and async connection/query helpers.

All schema is CREATE TABLE IF NOT EXISTS so later sessions need no migration.
Every connection enables foreign-key enforcement. All SQL is parameterized —
never interpolate values into a query string.
"""
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Default DB file sits in the project root; override with WSWRTB_DB_PATH (used by tests).
_DEFAULT_DB = Path(__file__).parent.parent / "wswrtb.db"
DB_PATH = Path(os.environ["WSWRTB_DB_PATH"]) if os.environ.get("WSWRTB_DB_PATH") else _DEFAULT_DB

# UTC timestamp format used for invite expiry. ISO-ish and lexicographically sortable,
# so string comparison in SQL (expires_at > ?) behaves like chronological comparison.
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# Membership roles. A group has exactly one owner (matching groups.owner_user_id);
# everyone else is a member.
ROLE_OWNER = "owner"
ROLE_MEMBER = "member"

# Account plans. 'free' is the only plan that exists today (users.plan defaults to it);
# it gates the monthly AI request allowance. No billing exists yet.
PLAN_FREE = "free"

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
    # Rate limiting (hardening pass): one row per throttled attempt. SQLite-backed so the
    # limit is shared across all Gunicorn worker processes and survives restarts.
    """CREATE TABLE IF NOT EXISTS rate_limit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

# Indexes created after the tables. Kept separate from _TABLES for clarity.
_INDEXES = [
    """CREATE INDEX IF NOT EXISTS idx_rate_limit_bucket_created
       ON rate_limit_events(bucket, created_at)""",
    # Session 3: every metered Claude call counts this user's recent rows (hour/day/month
    # windows) before issuing a request, and sums today's cost across ALL users. Both are
    # hot per-request reads, so index the two access patterns.
    """CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created
       ON ai_usage(user_id, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_ai_usage_created
       ON ai_usage(created_at)""",
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
            for ddl in _INDEXES:
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


async def get_role(user_id: int, group_id: int) -> str | None:
    """Return the user's role in the group (ROLE_OWNER/ROLE_MEMBER), or None if not a member."""
    async with connect() as db:
        async with db.execute(
            "SELECT role FROM memberships WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def list_user_groups(user_id: int) -> list[dict]:
    """Return the groups a user belongs to as {id, name, type, role}, ordered by name."""
    async with connect() as db:
        async with db.execute(
            """SELECT g.id, g.name, g.type, m.role
               FROM memberships m JOIN groups g ON g.id = m.group_id
               WHERE m.user_id = ?
               ORDER BY g.name, g.id""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [{"id": r[0], "name": r[1], "type": r[2], "role": r[3]} for r in rows]


# ---------------------------------------------------------------------------
# Group member management (Session 2)
# ---------------------------------------------------------------------------
async def list_members(group_id: int) -> list[dict]:
    """Return a group's members as {user_id, display_name, email, role, joined_at}.

    Ordered by join time (the owner, created first, sorts first). Email is included at
    the DB layer; routes decide whether to expose it (owners only).
    """
    async with connect() as db:
        async with db.execute(
            """SELECT u.id, u.display_name, u.email, m.role, m.created_at
               FROM memberships m JOIN users u ON u.id = m.user_id
               WHERE m.group_id = ?
               ORDER BY m.created_at, u.id""",
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {"user_id": r[0], "display_name": r[1], "email": r[2], "role": r[3], "joined_at": r[4]}
        for r in rows
    ]


async def get_group_member_profiles(group_id: int, user_ids: list[int]) -> list[dict]:
    """Return preference profiles for the given users, restricted to members of the group.

    Session 3: this is both the data source for recommendation prompts AND the
    cross-tenant guard — a user id that isn't a member of `group_id` simply produces no
    row, so the caller can compare the returned ids against what was requested and reject
    the request. Email is deliberately not selected; it never belongs in an AI prompt.

    The `IN (...)` placeholder list is generated from the id count — the ids themselves
    are still bound as `?` parameters, never interpolated.
    """
    if not user_ids:
        return []
    placeholders = ",".join("?" for _ in user_ids)
    async with connect() as db:
        async with db.execute(
            f"""SELECT u.id, u.display_name, u.favorite_genres, u.favorite_authors,
                       u.examples, u.dislikes, u.content_preferences, u.reading_pace,
                       u.preferred_length
                FROM memberships m JOIN users u ON u.id = m.user_id
                WHERE m.group_id = ? AND u.id IN ({placeholders})
                ORDER BY u.id""",
            (group_id, *user_ids),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "user_id": r[0],
            "display_name": r[1],
            "favorite_genres": json.loads(r[2]),
            "favorite_authors": json.loads(r[3]),
            "examples": json.loads(r[4]),
            "dislikes": json.loads(r[5]),
            "content_preferences": json.loads(r[6]),
            "reading_pace": r[7],
            "preferred_length": r[8],
        }
        for r in rows
    ]


async def remove_membership(user_id: int, group_id: int) -> bool:
    """Delete a user's membership in a group. Returns True if a row was removed.

    Only the membership link is deleted — the user's account and profile are untouched,
    so they simply lose access to this one group.
    """
    async with connect() as db:
        cur = await db.execute(
            "DELETE FROM memberships WHERE user_id = ? AND group_id = ?",
            (user_id, group_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Invite-code management (Session 2)
# ---------------------------------------------------------------------------
async def list_invite_codes(group_id: int) -> list[dict]:
    """Return all invite codes for a group, newest first, with seat accounting."""
    async with connect() as db:
        async with db.execute(
            """SELECT id, code, max_redemptions, redemption_count, active, created_at, expires_at
               FROM invite_codes WHERE group_id = ?
               ORDER BY created_at DESC, id DESC""",
            (group_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "code": r[1],
            "max_redemptions": r[2],
            "redemption_count": r[3],
            "active": r[4],
            "created_at": r[5],
            "expires_at": r[6],
        }
        for r in rows
    ]


async def create_invite_code(
    group_id: int, code: str, created_by_user_id: int, max_redemptions: int
) -> dict:
    """Insert a new active invite code for a group. Returns {id, code}.

    Raises aiosqlite.IntegrityError if the code already exists (UNIQUE violation).
    """
    async with connect() as db:
        cur = await db.execute(
            """INSERT INTO invite_codes (group_id, code, created_by_user_id, max_redemptions)
               VALUES (?, ?, ?, ?)""",
            (group_id, code, created_by_user_id, max_redemptions),
        )
        await db.commit()
        return {"id": cur.lastrowid, "code": code}


async def deactivate_invite_code(group_id: int, code_id: int) -> bool:
    """Force a code inactive (active = 0), independent of remaining seats.

    Returns True if the code belongs to the group (deactivating an already-inactive
    code is a harmless no-op that still returns True); False if no such code exists in
    the group. Scoping the UPDATE by group_id prevents cross-tenant deactivation.
    """
    async with connect() as db:
        cur = await db.execute(
            "UPDATE invite_codes SET active = 0 WHERE id = ? AND group_id = ?",
            (code_id, group_id),
        )
        await db.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# AI usage accounting (Session 3)
#
# `ai_usage` is the single source of truth for every AI cost control: the global daily
# spend ceiling and all three per-user request limits are derived by querying THIS table
# on every call. Nothing is cached in process memory — under Gunicorn each worker has its
# own memory, so an in-process counter would let every worker grant the full quota
# independently (the same reasoning that put `rate_limit_events` in SQLite).
# ---------------------------------------------------------------------------
async def record_ai_usage(
    *,
    user_id: int,
    group_id: int | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    est_cost_usd: float,
    endpoint: str | None,
) -> int:
    """Insert one row recording a completed Claude call. Returns the new row id.

    Token counts must come from the API response's usage field (never estimated), and
    `est_cost_usd` from the pricing constants in services/claude_service.py.
    """
    async with connect() as db:
        cur = await db.execute(
            """INSERT INTO ai_usage
                 (user_id, group_id, model, input_tokens, output_tokens, est_cost_usd, endpoint)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, group_id, model, input_tokens, output_tokens, est_cost_usd, endpoint),
        )
        await db.commit()
        return cur.lastrowid


async def ai_cost_today_usd() -> float:
    """Return the summed est_cost_usd of ALL users' calls so far in the current UTC day.

    The boundary is the UTC calendar day (`date('now')` in SQLite, which is UTC), not a
    rolling window: this backs the global kill switch, which is meant to reset each day.
    Comparing the stored 'YYYY-MM-DD HH:MM:SS' timestamp against 'YYYY-MM-DD' works
    because the format is lexicographically ordered.
    """
    async with connect() as db:
        async with db.execute(
            "SELECT COALESCE(SUM(est_cost_usd), 0) FROM ai_usage WHERE created_at >= date('now')"
        ) as cur:
            (total,) = await cur.fetchone()
    return float(total)


async def count_ai_usage_for_user(user_id: int, window_seconds: int) -> int:
    """Count a user's AI calls within the trailing `window_seconds`.

    Sliding window (like the abuse limiter), not a calendar bucket — so a user cannot
    burst a full day's quota either side of midnight. The window is evaluated in SQL so
    counting and the stored CURRENT_TIMESTAMP share one UTC clock (no Python/DB skew).
    """
    # Bound param, not string interpolation — the value is an int-coerced modifier string.
    window_modifier = f"-{int(window_seconds)} seconds"
    async with connect() as db:
        async with db.execute(
            """SELECT COUNT(*) FROM ai_usage
               WHERE user_id = ? AND created_at >= datetime('now', ?)""",
            (user_id, window_modifier),
        ) as cur:
            (count,) = await cur.fetchone()
    return int(count)


async def get_user_plan(user_id: int) -> str | None:
    """Return the user's plan ('free' for everyone today), or None if no such user."""
    async with connect() as db:
        async with db.execute("SELECT plan FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# External API response cache (Session 4)
#
# `api_cache` fronts third-party lookups (today: Google Books) so a repeated
# (title, author) never costs a second HTTP round trip. Like every other counter and
# cache in this app it lives in SQLite rather than process memory — under Gunicorn each
# worker has its own memory, so an in-process dict would be cached per-worker and lost
# on restart. The key is namespaced by provider; see services/google_books.py.
# ---------------------------------------------------------------------------
async def get_api_cache(cache_key: str) -> dict | None:
    """Return the cached payload for a key if present and unexpired, else None.

    An expired row is treated exactly like a miss (the caller re-fetches and overwrites
    it), so nothing here has to delete rows on a read path.
    """
    async with connect() as db:
        async with db.execute(
            "SELECT response_json FROM api_cache WHERE cache_key = ? AND expires_at > ?",
            (cache_key, utcnow_str()),
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        # A corrupt row must not break a lookup — treat it as a miss and let the
        # caller overwrite it on the next successful fetch.
        logger.warning("Discarding unparseable api_cache row for %s", cache_key)
        return None


async def set_api_cache(cache_key: str, payload: dict, ttl_days: int) -> None:
    """Insert or replace one cached API payload, computing `expires_at` from `ttl_days`.

    Upsert rather than insert: a re-fetch after expiry refreshes the same key in place,
    so the table holds at most one row per distinct lookup.
    """
    now = datetime.now(timezone.utc)
    async with connect() as db:
        await db.execute(
            """INSERT INTO api_cache (cache_key, response_json, cached_at, expires_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 response_json = excluded.response_json,
                 cached_at = excluded.cached_at,
                 expires_at = excluded.expires_at""",
            (
                cache_key,
                json.dumps(payload),
                format_ts(now),
                format_ts(now + timedelta(days=ttl_days)),
            ),
        )
        await db.commit()


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
