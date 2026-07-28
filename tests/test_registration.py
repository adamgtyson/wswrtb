"""Code-gated registration and seat-limit logic."""
from datetime import datetime, timedelta, timezone

from app import db

from .conftest import register


def _past_ts() -> str:
    return db.format_ts(datetime.now(timezone.utc) - timedelta(days=1))


# ----- Rejection paths -----
def test_missing_code_rejected(client, seed):
    """A registration with no invite_code field fails validation (422)."""
    seed(code="NIGHTOWLS", seats=8)
    res = client.post(
        "/api/register",
        json={"email": "a@example.com", "password": "password1", "display_name": "A"},
    )
    assert res.status_code == 422


def test_empty_code_rejected(client, seed):
    """An empty invite_code is rejected."""
    seed(code="NIGHTOWLS", seats=8)
    res = register(client, code="", email="a@example.com")
    assert res.status_code == 422


def test_invalid_code_rejected(client, seed):
    """A well-formed but nonexistent code is rejected with the generic error."""
    seed(code="NIGHTOWLS", seats=8)
    res = register(client, code="NOPE-NOPE", email="a@example.com")
    assert res.status_code == 400
    assert res.json()["detail"] == "That invite code is invalid or full."


def test_expired_code_rejected(client, seed):
    """A code past its expiry cannot be redeemed."""
    seed(code="EXPIRED-1", seats=8, expires_at=_past_ts())
    res = register(client, code="EXPIRED-1", email="a@example.com")
    assert res.status_code == 400
    # No seat consumed on a rejected redemption.
    row = _redemption_state(client, "EXPIRED-1")
    assert row["redemption_count"] == 0


# ----- Success path -----
def test_valid_code_creates_user_and_membership(client, seed, fetchone):
    """A valid code registers the user, links a member membership, and logs them in."""
    info = seed(code="NIGHTOWLS", seats=8)
    res = register(client, code="NIGHTOWLS", email="member@example.com")
    assert res.status_code == 201
    assert res.cookies.get("session")  # session cookie set

    user = fetchone("SELECT id FROM users WHERE email = ?", ("member@example.com",))
    assert user is not None
    membership = fetchone(
        "SELECT role FROM memberships WHERE user_id = ? AND group_id = ?",
        (user[0], info["group_id"]),
    )
    assert membership is not None and membership[0] == "member"


def test_code_normalizes_case_and_whitespace(client, seed):
    """A code typed in a different case / with spaces still matches."""
    seed(code="NIGHTOWLS-2026", seats=8)
    res = register(client, code="  nightowls-2026  ", email="member@example.com")
    assert res.status_code == 201


# ----- Seat accounting -----
def _redemption_state(client, code):
    """Read a code's redemption_count and active flag directly from the DB."""
    import asyncio

    async def go():
        async with db.connect() as conn:
            async with conn.execute(
                "SELECT redemption_count, active, max_redemptions FROM invite_codes WHERE code = ?",
                (code,),
            ) as cur:
                row = await cur.fetchone()
        return {"redemption_count": row[0], "active": row[1], "max_redemptions": row[2]}

    return asyncio.run(go())


def test_redemption_count_increments(client, seed):
    """Each successful registration increments redemption_count."""
    seed(code="CLUB4", seats=4)
    register(client, code="CLUB4", email="one@example.com")
    register(client, code="CLUB4", email="two@example.com")
    state = _redemption_state(client, "CLUB4")
    assert state["redemption_count"] == 2
    assert state["active"] == 1  # still seats left


def test_code_deactivates_at_cap_and_ninth_is_refused(client, seed):
    """An 8-seat code fills to 8, deactivates, and refuses the 9th registration."""
    seed(code="EIGHT", seats=8)
    for i in range(8):
        res = register(client, code="EIGHT", email=f"user{i}@example.com")
        assert res.status_code == 201, f"seat {i + 1} should succeed"

    state = _redemption_state(client, "EIGHT")
    assert state["redemption_count"] == 8
    assert state["active"] == 0  # auto-deactivated at the cap

    ninth = register(client, code="EIGHT", email="ninth@example.com")
    assert ninth.status_code == 400
    assert ninth.json()["detail"] == "That invite code is invalid or full."
    # The refused 9th did not create a user.
    assert _redemption_state(client, "EIGHT")["redemption_count"] == 8


# ----- Duplicate protection -----
def test_duplicate_email_rejected_without_consuming_seat(client, seed):
    """Re-registering the same email is refused and does not burn a seat."""
    seed(code="DUPE", seats=8)
    first = register(client, code="DUPE", email="taken@example.com")
    assert first.status_code == 201
    assert _redemption_state(client, "DUPE")["redemption_count"] == 1

    second = register(client, code="DUPE", email="taken@example.com")
    assert second.status_code == 400
    assert second.json()["detail"] == "That invite code is invalid or full."
    # Seat count unchanged — the failed insert rolled back the increment.
    assert _redemption_state(client, "DUPE")["redemption_count"] == 1


def test_user_cannot_redeem_same_code_twice(client, seed, fetchone):
    """The invite_redemptions UNIQUE(invite_code_id, user_id) blocks a double redemption."""
    import asyncio

    import aiosqlite

    info = seed(code="ONCE", seats=8)
    register(client, code="ONCE", email="once@example.com")
    user = fetchone("SELECT id FROM users WHERE email = ?", ("once@example.com",))
    invite = fetchone("SELECT id FROM invite_codes WHERE code = ?", ("ONCE",))

    async def double_insert():
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO invite_redemptions (invite_code_id, user_id) VALUES (?, ?)",
                (invite[0], user[0]),
            )
            await conn.commit()

    raised = False
    try:
        asyncio.run(double_insert())
    except aiosqlite.IntegrityError:
        raised = True
    assert raised, "second redemption of the same code by the same user must fail"
