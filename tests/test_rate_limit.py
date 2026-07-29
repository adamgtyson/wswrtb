"""SQLite-backed rate limiter: helper trip/reset behavior and endpoint 429 wiring.

Functional tests run with generous limits (set in conftest), so these tests either call
the helper directly with explicit limits or monkeypatch the module constants low to
observe a trip. Routes read the limits via `rate_limit.<CONST>` (module attribute), so
monkeypatching the module attribute takes effect at request time.
"""
import asyncio

from app import db
from app.services import rate_limit
from app.services.rate_limit import RateLimitError, check_and_record

from .conftest import OWNER_PASSWORD, register


def _run(coro):
    return asyncio.run(coro)


def _age_bucket(bucket, seconds):
    """Push a bucket's rows `seconds` into the past so they fall outside a window."""

    async def go():
        async with db.connect() as conn:
            await conn.execute(
                "UPDATE rate_limit_events SET created_at = datetime('now', ?) WHERE bucket = ?",
                (f"-{int(seconds)} seconds", bucket),
            )
            await conn.commit()

    _run(go())


# ----- Helper unit behavior -----
def test_helper_trips_at_limit(client):
    """Calls under the limit are recorded; the call at the limit raises."""

    async def go():
        for _ in range(3):
            await check_and_record("unit:a", 3, 100)
        try:
            await check_and_record("unit:a", 3, 100)
            return False
        except RateLimitError:
            return True

    assert _run(go()) is True


def test_helper_resets_after_window(client):
    """Events older than the window are pruned and no longer count."""

    async def fill():
        for _ in range(3):
            await check_and_record("unit:b", 3, 100)

    _run(fill())
    _age_bucket("unit:b", 200)  # push past the 100s window
    # Should NOT raise now that the earlier events have aged out.
    _run(check_and_record("unit:b", 3, 100))


def test_buckets_are_independent(client):
    """Filling one bucket does not throttle another."""

    async def go():
        for _ in range(3):
            await check_and_record("unit:c", 3, 100)
        await check_and_record("unit:d", 3, 100)  # different bucket -> allowed

    _run(go())


# ----- Endpoint wiring -----
def test_register_endpoint_returns_429(client, seed, monkeypatch):
    """Registration is throttled by client IP once the limit is exceeded."""
    seed(code="RLREGISTER", seats=50)
    monkeypatch.setattr(rate_limit, "REGISTER_LIMIT", 3)
    monkeypatch.setattr(rate_limit, "REGISTER_WINDOW_SECONDS", 3600)

    for i in range(3):
        assert register(client, code="RLREGISTER", email=f"u{i}@example.com").status_code == 201
    throttled = register(client, code="RLREGISTER", email="u4@example.com")
    assert throttled.status_code == 429
    assert throttled.json()["detail"]  # generic message present


def test_login_endpoint_returns_429(client, seed, monkeypatch):
    """Login is throttled by client IP."""
    seed(code="RLLOGIN", seats=8)
    register(client, code="RLLOGIN", email="log@example.com", password="password1")
    monkeypatch.setattr(rate_limit, "LOGIN_LIMIT", 2)
    monkeypatch.setattr(rate_limit, "LOGIN_WINDOW_SECONDS", 900)

    creds = {"email": "log@example.com", "password": "password1"}
    for _ in range(2):
        assert client.post("/api/login", json=creds).status_code == 200
    assert client.post("/api/login", json=creds).status_code == 429


def test_invite_create_endpoint_returns_429(client, seed, monkeypatch):
    """Invite-code creation is throttled by owner user id (on top of require_owner)."""
    info = seed(code="RLINVITE", seats=8)
    client.post("/api/login", json={"email": "owner@example.com", "password": OWNER_PASSWORD})
    monkeypatch.setattr(rate_limit, "INVITE_CREATE_LIMIT", 2)
    monkeypatch.setattr(rate_limit, "INVITE_CREATE_WINDOW_SECONDS", 3600)

    gid = info["group_id"]
    for _ in range(2):
        assert client.post(f"/api/groups/{gid}/invite-codes", json={"max_redemptions": 5}).status_code == 201
    assert client.post(f"/api/groups/{gid}/invite-codes", json={"max_redemptions": 5}).status_code == 429
