"""Thumbs up/down feedback: upsert semantics, toggle-style delete, lookup, limits.

Feedback is per-user and account-global — the `feedback` table has no group_id — so these
tests authorize on authentication alone and assert that the UNIQUE(user_id, title) key is
what keeps a re-rated book to exactly one row.

Nothing here should reach Claude or Google Books: rating a book spends nothing. The
autouse Google Books stub is in force as always, and no test installs `fake_claude`.
"""
import asyncio

import pytest

from app import db
from app.services import rate_limit

from .conftest import OWNER_PASSWORD, register


def _run(coro):
    return asyncio.run(coro)


def _fetchall(sql, params=()):
    """Run a parameterized SELECT and return every row."""

    async def go():
        async with db.connect() as conn:
            async with conn.execute(sql, params) as cur:
                return await cur.fetchall()

    return _run(go())


def _rate(client, title, rating, author="Some Author", google_books_id="gb-1"):
    payload = {"title": title, "rating": rating}
    if author is not None:
        payload["author"] = author
    if google_books_id is not None:
        payload["google_books_id"] = google_books_id
    return client.put("/api/feedback", json=payload)


def _login_owner(client):
    return client.post(
        "/api/login", json={"email": "owner@example.com", "password": OWNER_PASSWORD}
    )


@pytest.fixture()
def owner(client, seed):
    """A logged-in owner. Returns the owner's ids."""
    info = seed(code="RATECLUB", seats=8)
    _login_owner(client)
    return info


# ---------------------------------------------------------------------------
# Create / update / idempotence
# ---------------------------------------------------------------------------
def test_rate_creates_a_row(client, owner):
    """A thumbs up stores exactly one row with the submitted metadata."""
    res = _rate(client, "Dune", 1)

    assert res.status_code == 200
    assert res.json() == {"title": "Dune", "rating": 1}
    rows = _fetchall("SELECT user_id, title, author, google_books_id, rating FROM feedback")
    assert len(rows) == 1
    assert rows[0][0] == owner["owner_user_id"]
    assert rows[0][1] == "Dune"
    assert rows[0][2] == "Some Author"
    assert rows[0][3] == "gb-1"
    assert rows[0][4] == 1


def test_flipping_the_rating_updates_the_same_row(client, owner):
    """Re-rating a title the other way updates it — the UNIQUE key forbids a duplicate."""
    _rate(client, "Dune", 1)
    res = _rate(client, "Dune", -1)

    assert res.status_code == 200
    assert res.json()["rating"] == -1
    rows = _fetchall("SELECT rating FROM feedback WHERE title = ?", ("Dune",))
    assert rows == [(-1,)]


def test_rating_the_same_direction_twice_is_idempotent(client, owner):
    """Submitting an unchanged rating succeeds and leaves one row — not an error."""
    assert _rate(client, "Dune", 1).status_code == 200
    assert _rate(client, "Dune", 1).status_code == 200

    rows = _fetchall("SELECT rating FROM feedback WHERE title = ?", ("Dune",))
    assert rows == [(1,)]


def test_omitted_metadata_does_not_blank_stored_values(client, owner):
    """A later rating without author/id keeps what the first one recorded (COALESCE)."""
    _rate(client, "Dune", 1, author="Frank Herbert", google_books_id="gb-dune")
    _rate(client, "Dune", -1, author=None, google_books_id=None)

    rows = _fetchall("SELECT author, google_books_id, rating FROM feedback WHERE title = ?", ("Dune",))
    assert rows == [("Frank Herbert", "gb-dune", -1)]


def test_title_is_stripped_before_storage(client, owner):
    """Whitespace around a title can't create a second row for the same book."""
    _rate(client, "Dune", 1)
    _rate(client, "  Dune  ", -1)

    rows = _fetchall("SELECT title, rating FROM feedback")
    assert rows == [("Dune", -1)]


def test_two_users_rate_the_same_title_independently(client, owner, fetchone):
    """The uniqueness key is (user_id, title) — one book, one row per member."""
    _rate(client, "Dune", 1)
    client.post("/api/logout")
    register(client, code="RATECLUB", email="reader@example.com", display_name="Reader")
    _rate(client, "Dune", -1)

    rows = _fetchall("SELECT user_id, rating FROM feedback WHERE title = ? ORDER BY user_id", ("Dune",))
    assert len(rows) == 2
    assert {r[1] for r in rows} == {1, -1}


# ---------------------------------------------------------------------------
# Delete (toggle)
# ---------------------------------------------------------------------------
def test_delete_clears_the_rating(client, owner):
    """Un-toggling removes the row."""
    _rate(client, "Dune", 1)
    res = client.delete("/api/feedback", params={"title": "Dune"})

    assert res.status_code == 204
    assert _fetchall("SELECT * FROM feedback") == []


def test_delete_nonexistent_rating_is_204(client, owner):
    """Clearing a rating that was never set succeeds — this is a toggle, not a resource."""
    res = client.delete("/api/feedback", params={"title": "Never Rated"})

    assert res.status_code == 204
    assert _fetchall("SELECT * FROM feedback") == []


def test_delete_only_touches_the_callers_own_rating(client, owner):
    """One member clearing a rating leaves another member's rating of the same book."""
    _rate(client, "Dune", 1)
    client.post("/api/logout")
    register(client, code="RATECLUB", email="reader@example.com", display_name="Reader")
    _rate(client, "Dune", -1)
    client.delete("/api/feedback", params={"title": "Dune"})

    rows = _fetchall("SELECT user_id, rating FROM feedback")
    assert len(rows) == 1
    assert rows[0][0] == owner["owner_user_id"]


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------
def test_lookup_returns_only_rated_titles(client, owner):
    """Unrated titles are absent from the map rather than present with a zero."""
    _rate(client, "Dune", 1)
    _rate(client, "Neuromancer", -1)

    res = client.get("/api/feedback", params={"titles": ["Dune", "Neuromancer", "Unrated"]})

    assert res.status_code == 200
    assert res.json()["ratings"] == {"Dune": 1, "Neuromancer": -1}


def test_lookup_excludes_another_users_ratings(client, owner):
    """A member never sees what anyone else rated."""
    _rate(client, "Dune", 1)
    client.post("/api/logout")
    register(client, code="RATECLUB", email="reader@example.com", display_name="Reader")
    _rate(client, "Neuromancer", 1)

    res = client.get("/api/feedback", params={"titles": ["Dune", "Neuromancer"]})

    assert res.json()["ratings"] == {"Neuromancer": 1}


def test_lookup_with_no_titles_returns_an_empty_map(client, owner):
    """No titles asked about is an empty map, not an error."""
    res = client.get("/api/feedback")

    assert res.status_code == 200
    assert res.json()["ratings"] == {}


def test_lookup_rejects_too_many_titles(client, owner):
    """The title list is bounded so one query string can't ask about everything."""
    res = client.get("/api/feedback", params={"titles": [f"Book {i}" for i in range(60)]})

    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Validation and auth
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_rating", [0, 2, -2, "up", None])
def test_invalid_rating_is_rejected(client, owner, bad_rating):
    """Only -1 and 1 are ratings; everything else is a 422 before any DB write."""
    res = client.put("/api/feedback", json={"title": "Dune", "rating": bad_rating})

    assert res.status_code == 422
    assert _fetchall("SELECT * FROM feedback") == []


def test_blank_title_is_rejected(client, owner):
    """A whitespace-only title would make a rating nothing can refer to."""
    res = client.put("/api/feedback", json={"title": "   ", "rating": 1})

    assert res.status_code == 422


def test_overlong_title_is_rejected(client, owner):
    """Titles are bounded server-side like every other stored string."""
    res = client.put("/api/feedback", json={"title": "x" * 400, "rating": 1})

    assert res.status_code == 422


def test_rating_requires_authentication(client, seed):
    """No session, no rating — 401 JSON on an /api/ path."""
    seed(code="RATECLUB", seats=8)
    res = client.put("/api/feedback", json={"title": "Dune", "rating": 1})

    assert res.status_code == 401
    assert _fetchall("SELECT * FROM feedback") == []


def test_delete_requires_authentication(client, seed):
    """The clear path is authenticated too."""
    seed(code="RATECLUB", seats=8)
    assert client.delete("/api/feedback", params={"title": "Dune"}).status_code == 401


def test_lookup_requires_authentication(client, seed):
    """Ratings are private to the member who made them."""
    seed(code="RATECLUB", seats=8)
    assert client.get("/api/feedback", params={"titles": ["Dune"]}).status_code == 401


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_feedback_writes_are_rate_limited(client, owner, monkeypatch):
    """The write path trips the shared limiter and returns the generic 429."""
    monkeypatch.setattr(rate_limit, "FEEDBACK_LIMIT", 3)

    for i in range(3):
        assert _rate(client, f"Book {i}", 1).status_code == 200

    res = _rate(client, "One Too Many", 1)
    assert res.status_code == 429
    assert "Too many requests" in res.json()["detail"]
    assert _fetchall("SELECT * FROM feedback WHERE title = ?", ("One Too Many",)) == []


def test_delete_shares_the_feedback_bucket(client, owner, monkeypatch):
    """Clearing a rating counts against the same per-user bucket as setting one."""
    monkeypatch.setattr(rate_limit, "FEEDBACK_LIMIT", 2)

    assert _rate(client, "Dune", 1).status_code == 200
    assert client.delete("/api/feedback", params={"title": "Dune"}).status_code == 204
    assert client.delete("/api/feedback", params={"title": "Dune"}).status_code == 429


def test_lookup_is_not_rate_limited(client, owner, monkeypatch):
    """Reads are cheap and repeated on every render, so only writes are throttled."""
    monkeypatch.setattr(rate_limit, "FEEDBACK_LIMIT", 1)
    _rate(client, "Dune", 1)

    for _ in range(5):
        assert client.get("/api/feedback", params={"titles": ["Dune"]}).status_code == 200
