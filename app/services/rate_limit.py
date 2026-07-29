"""SQLite-backed sliding-window rate limiting.

Why not an in-memory limiter (or a library's default in-process store): under Gunicorn
each worker process has its own memory, so an in-process counter lets every worker grant
the full quota independently, and all counters reset on restart. This limiter records
each attempt as a row in `rate_limit_events` and counts rows in a trailing time window,
so every worker shares one source of truth (the SQLite file) and limits survive restarts.

Concurrency: the count-then-insert runs inside a single `BEGIN IMMEDIATE` transaction
(the same pattern used for atomic invite redemption). BEGIN IMMEDIATE takes the write
lock up front, so two requests racing on the same bucket are serialized — the second
sees the first's inserted row and cannot slip past the limit.

Cleanup: each call opportunistically deletes the bucket's own rows older than the window,
so any bucket that keeps receiving traffic self-prunes. A bucket that goes silent leaves
a handful of stale rows until it is next touched; at book-club scale that is negligible.

All limits/windows are configurable via environment variables (below) with sane defaults,
so they can be tuned without code changes.
"""
import logging
import os

from app import db

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back to `default`."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Per-action limits (max attempts) and windows (seconds). Placeholders — tune with real
# usage. Buckets are keyed by client IP for pre-auth endpoints and by user id for
# authenticated ones.
REGISTER_LIMIT = _int_env("RATE_LIMIT_REGISTER", 5)
REGISTER_WINDOW_SECONDS = _int_env("RATE_LIMIT_REGISTER_WINDOW", 60 * 60)  # 1 hour

LOGIN_LIMIT = _int_env("RATE_LIMIT_LOGIN", 10)
LOGIN_WINDOW_SECONDS = _int_env("RATE_LIMIT_LOGIN_WINDOW", 15 * 60)  # 15 minutes

INVITE_CREATE_LIMIT = _int_env("RATE_LIMIT_INVITE_CREATE", 20)
INVITE_CREATE_WINDOW_SECONDS = _int_env("RATE_LIMIT_INVITE_CREATE_WINDOW", 60 * 60)  # 1 hour


class RateLimitError(Exception):
    """Raised when a bucket exceeds its limit within the window. Mapped to HTTP 429.

    Carries no timing/quota detail so the API response stays generic, consistent with the
    project's other auth/registration errors.
    """


def client_ip(request) -> str:
    """Best-effort client IP for pre-auth rate-limit buckets.

    Uses the direct socket peer (`request.client.host`). NOTE: once this app sits behind
    Nginx in production (Session 7 deployment), the real client IP will arrive in the
    `X-Forwarded-For` header and this must be updated to read it — trusting only the
    proxy. Until that proxy exists, honoring X-Forwarded-For would let any client spoof
    its IP and evade the limit, so we deliberately do NOT read it yet.
    """
    return request.client.host if request.client else "unknown"


async def check_and_record(bucket: str, limit: int, window_seconds: int) -> None:
    """Enforce `limit` events per `window_seconds` for `bucket`.

    Prunes the bucket's expired rows, counts remaining events in the trailing window, and
    either records this event (under the limit) or raises RateLimitError (at/over it). The
    window is evaluated in SQL via ``datetime('now', ?)`` so counting, pruning, and the
    stored CURRENT_TIMESTAMP all share one UTC clock (no Python/DB skew).
    """
    # Bound param, not string interpolation — keeps the SQL parameterized. The value is an
    # int-coerced modifier string like "-3600 seconds".
    window_modifier = f"-{int(window_seconds)} seconds"

    async with db.connect(isolation_level=None) as conn:  # manual transaction control
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                "DELETE FROM rate_limit_events WHERE bucket = ? AND created_at < datetime('now', ?)",
                (bucket, window_modifier),
            )
            async with conn.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE bucket = ? AND created_at >= datetime('now', ?)",
                (bucket, window_modifier),
            ) as cur:
                (count,) = await cur.fetchone()

            over_limit = count >= limit
            if not over_limit:
                await conn.execute(
                    "INSERT INTO rate_limit_events (bucket) VALUES (?)", (bucket,)
                )
            await conn.execute("COMMIT")
        except Exception:
            await conn.execute("ROLLBACK")
            raise

    if over_limit:
        raise RateLimitError()
