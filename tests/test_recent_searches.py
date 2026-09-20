"""Recent searches: the history write, private listing, and replay without spend.

The point of this feature is that re-opening a past result set costs nothing, so the
central assertion throughout is on the FAKE CLAUDE CLIENT'S CALL COUNT. The fake asserts
on any call it has no queued response for, so a replay that secretly re-asked Claude would
fail loudly rather than quietly costing money.

Google Books is stubbed suite-wide by the autouse fixture; a replay must not reach it
either, and does not — stored results are returned as stored, never re-verified.
"""
import asyncio
import json

import pytest

from app import db
from app.routes import recommend_routes
from app.services import claude_service

from .conftest import OWNER_PASSWORD, claude_response, register


def _run(coro):
    return asyncio.run(coro)


def _fetchall(sql, params=()):
    """Run a parameterized SELECT and return every row."""

    async def go():
        async with db.connect() as conn:
            async with conn.execute(sql, params) as cur:
                return await cur.fetchall()

    return _run(go())


def _books(*titles):
    """Build a JSON array body with one entry per title."""
    return json.dumps(
        [
            {
                "title": t,
                "author": f"Author of {t}",
                "year": 2000 + i,
                "reason": "Suits the group.",
            }
            for i, t in enumerate(titles)
        ]
    )


FIVE = _books("Alpha", "Bravo", "Charlie", "Delta", "Echo")


@pytest.fixture()
def club(client, seed, fetchone):
    """A logged-in owner with one other member in the same group."""
    info = seed(code="HISTCLUB", seats=8)
    register(client, code="HISTCLUB", email="reader@example.com", display_name="Reader")
    member_id = fetchone("SELECT id FROM users WHERE email = ?", ("reader@example.com",))[0]
    client.post("/api/login", json={"email": "owner@example.com", "password": OWNER_PASSWORD})
    return {
        "group_id": info["group_id"],
        "owner_id": info["owner_user_id"],
        "member_id": member_id,
    }


def _ask(client, group_id, member_ids, prompt="Something twisty and cold"):
    return client.post(
        f"/api/groups/{group_id}/recommend",
        json={"prompt": prompt, "member_user_ids": member_ids},
    )


def _login_reader(client):
    client.post("/api/logout")
    return client.post(
        "/api/login", json={"email": "reader@example.com", "password": "password1"}
    )


# ---------------------------------------------------------------------------
# The history write
# ---------------------------------------------------------------------------
def test_successful_recommend_writes_exactly_one_row(client, club, fake_claude):
    """One 200 response, one history row, with the right count and readers."""
    fake_claude(claude_response(FIVE))
    res = _ask(client, club["group_id"], [club["owner_id"], club["member_id"]])
    assert res.status_code == 200

    rows = _fetchall(
        "SELECT user_id, group_id, prompt, result_count, watching, results_json FROM recent_searches"
    )
    assert len(rows) == 1
    user_id, group_id, prompt, result_count, watching, results_json = rows[0]
    assert user_id == club["owner_id"]
    assert group_id == club["group_id"]
    assert prompt == "Something twisty and cold"
    assert result_count == 5
    assert sorted(json.loads(watching)) == ["Owner", "Reader"]
    assert [r["title"] for r in json.loads(results_json)] == [
        "Alpha", "Bravo", "Charlie", "Delta", "Echo"
    ]


def test_watching_records_only_the_selected_readers(client, club, fake_claude):
    """`watching` is who was reading, not the whole roster."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])

    (watching,) = _fetchall("SELECT watching FROM recent_searches")[0]
    assert json.loads(watching) == ["Owner"]


def test_stored_results_carry_the_verified_metadata(client, club, fake_claude):
    """The stored payload is the full response, so a replay needs no second lookup."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])

    (results_json,) = _fetchall("SELECT results_json FROM recent_searches")[0]
    first = json.loads(results_json)[0]
    assert first["verified"] is True
    assert first["google_books_id"]
    assert first["thumbnail_url"].startswith("https://")


def test_failed_recommend_writes_no_history(client, club, fake_claude):
    """An upstream failure is a 502 and leaves no row — history tracks what was delivered."""
    fake_claude(claude_service.ClaudeServiceError("upstream is down"))
    res = _ask(client, club["group_id"], [club["owner_id"]])

    assert res.status_code == 502
    assert _fetchall("SELECT * FROM recent_searches") == []


def test_rejected_member_ids_write_no_history(client, club, seed, fake_claude, fetchone):
    """A cross-tenant id is refused before any spend, so there is nothing to record."""
    seed(code="OTHERCLUB", seats=4, email="other@example.com", group_name="Other Club")
    outsider_id = fetchone("SELECT id FROM users WHERE email = ?", ("other@example.com",))[0]
    fake_claude()  # any Claude call at all would fail this test

    res = _ask(client, club["group_id"], [club["owner_id"], outsider_id])

    assert res.status_code == 400
    assert _fetchall("SELECT * FROM recent_searches") == []


def test_history_failure_does_not_fail_the_request(client, club, fake_claude, monkeypatch):
    """The member has already been charged for the call — history must never 500 them."""
    fake_claude(claude_response(FIVE))

    async def boom(**kwargs):
        raise RuntimeError("disk is on fire")

    monkeypatch.setattr(db, "record_recent_search", boom)

    res = _ask(client, club["group_id"], [club["owner_id"]])

    assert res.status_code == 200
    assert res.json()["count"] == 5
    assert _fetchall("SELECT * FROM recent_searches") == []


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------
def test_listing_returns_newest_first_without_results(client, club, fake_claude):
    """The menu carries summaries only; the payload is fetched on demand."""
    fake_claude(claude_response(FIVE), claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]], prompt="First ask about ships")
    _ask(client, club["group_id"], [club["owner_id"]], prompt="Second ask about trains")

    res = client.get(f"/api/groups/{club['group_id']}/recent-searches")

    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 2
    assert body["searches"][0]["prompt"] == "Second ask about trains"
    assert body["searches"][1]["prompt"] == "First ask about ships"
    assert body["searches"][0]["result_count"] == 5
    assert body["searches"][0]["watching"] == ["Owner"]
    assert "results_json" not in body["searches"][0]
    assert "recommendations" not in body["searches"][0]


def test_listing_excludes_another_members_searches(client, club, fake_claude):
    """History is private to the member who ran it, even inside one group."""
    fake_claude(claude_response(FIVE), claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]], prompt="Owner's own ask")
    _login_reader(client)
    _ask(client, club["group_id"], [club["member_id"]], prompt="Reader's own ask")

    res = client.get(f"/api/groups/{club['group_id']}/recent-searches")

    assert res.status_code == 200
    prompts = [s["prompt"] for s in res.json()["searches"]]
    assert prompts == ["Reader's own ask"]


def test_listing_requires_membership(client, club, seed, fake_claude):
    """A non-member of the group gets 403, not someone else's history."""
    seed(code="OTHERCLUB", seats=4, email="other@example.com", group_name="Other Club")
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])

    client.post("/api/logout")
    client.post("/api/login", json={"email": "other@example.com", "password": OWNER_PASSWORD})
    res = client.get(f"/api/groups/{club['group_id']}/recent-searches")

    assert res.status_code == 403


def test_listing_requires_authentication(client, club):
    """No session, no history."""
    client.post("/api/logout")
    assert client.get(f"/api/groups/{club['group_id']}/recent-searches").status_code == 401


def test_listing_is_capped_at_the_display_limit(client, club, fake_claude, monkeypatch):
    """At most RECENT_SEARCHES_LIMIT entries come back however many were run."""
    monkeypatch.setattr(recommend_routes, "RECENT_SEARCHES_LIMIT", 3)
    fake_claude(*[claude_response(FIVE) for _ in range(5)])
    for i in range(5):
        _ask(client, club["group_id"], [club["owner_id"]], prompt=f"Ask number {i}")

    res = client.get(f"/api/groups/{club['group_id']}/recent-searches")

    assert res.json()["count"] == 3
    assert res.json()["searches"][0]["prompt"] == "Ask number 4"


# ---------------------------------------------------------------------------
# Replay — the whole point: no AI call
# ---------------------------------------------------------------------------
def test_replay_returns_stored_results_and_spends_nothing(client, club, fake_claude):
    """Re-opening a search returns the same books and makes ZERO Claude calls."""
    fake = fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"], club["member_id"]])
    assert fake.call_count == 1

    usage_before = _fetchall("SELECT COUNT(*) FROM ai_usage")[0][0]
    search_id = client.get(f"/api/groups/{club['group_id']}/recent-searches").json()[
        "searches"
    ][0]["id"]

    res = client.get(f"/api/groups/{club['group_id']}/recent-searches/{search_id}")

    assert res.status_code == 200
    body = res.json()
    assert [r["title"] for r in body["recommendations"]] == [
        "Alpha", "Bravo", "Charlie", "Delta", "Echo"
    ]
    assert body["result_count"] == 5
    assert body["prompt"] == "Something twisty and cold"
    assert sorted(body["watching"]) == ["Owner", "Reader"]
    assert body["created_at"]  # the client is required to show this
    # Nothing was spent: no extra API call, no new usage row.
    assert fake.call_count == 1
    assert _fetchall("SELECT COUNT(*) FROM ai_usage")[0][0] == usage_before


def test_replay_preserves_the_verified_metadata(client, club, fake_claude):
    """Covers and page counts survive the round trip, so cards render identically."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    search_id = client.get(f"/api/groups/{club['group_id']}/recent-searches").json()[
        "searches"
    ][0]["id"]

    first = client.get(
        f"/api/groups/{club['group_id']}/recent-searches/{search_id}"
    ).json()["recommendations"][0]

    assert first["verified"] is True
    assert first["thumbnail_url"]
    assert first["page_count"]
    assert first["canonical_title"]


def test_replay_of_another_members_search_is_404(client, club, fake_claude):
    """Guessing an id belonging to another member returns nothing."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    search_id = client.get(f"/api/groups/{club['group_id']}/recent-searches").json()[
        "searches"
    ][0]["id"]

    _login_reader(client)
    res = client.get(f"/api/groups/{club['group_id']}/recent-searches/{search_id}")

    assert res.status_code == 404


def test_replay_across_groups_is_404(client, club, seed, fake_claude, fetchone):
    """The caller's own search is still not readable through a different group's path."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    search_id = client.get(f"/api/groups/{club['group_id']}/recent-searches").json()[
        "searches"
    ][0]["id"]

    # Put the same user in a second group, then ask for the search through that group.
    second = seed(code="SECONDCLUB", seats=4, email="second@example.com", group_name="Second Club")
    async def add_membership():
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO memberships (user_id, group_id, role) VALUES (?, ?, 'member')",
                (club["owner_id"], second["group_id"]),
            )
            await conn.commit()

    _run(add_membership())

    res = client.get(f"/api/groups/{second['group_id']}/recent-searches/{search_id}")

    assert res.status_code == 404


def test_replay_of_an_unknown_id_is_404(client, club):
    """A search that never existed is a 404, not a 500."""
    res = client.get(f"/api/groups/{club['group_id']}/recent-searches/999999")
    assert res.status_code == 404


def test_replay_requires_membership(client, club, seed, fake_claude):
    """A non-member cannot reach the replay path at all."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    search_id = client.get(f"/api/groups/{club['group_id']}/recent-searches").json()[
        "searches"
    ][0]["id"]

    seed(code="OTHERCLUB", seats=4, email="other@example.com", group_name="Other Club")
    client.post("/api/logout")
    client.post("/api/login", json={"email": "other@example.com", "password": OWNER_PASSWORD})

    res = client.get(f"/api/groups/{club['group_id']}/recent-searches/{search_id}")
    assert res.status_code == 403


def test_replay_drops_unusable_stored_entries(client, club, fake_claude):
    """A stored row from an older shape is re-validated, not trusted — bad entries drop."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])

    async def corrupt():
        async with db.connect() as conn:
            await conn.execute(
                "UPDATE recent_searches SET results_json = ?",
                (json.dumps([{"title": "Good", "author": "A", "reason": "R"}, {"nope": 1}]),),
            )
            await conn.commit()

    _run(corrupt())
    search_id = client.get(f"/api/groups/{club['group_id']}/recent-searches").json()[
        "searches"
    ][0]["id"]

    body = client.get(
        f"/api/groups/{club['group_id']}/recent-searches/{search_id}"
    ).json()

    assert [r["title"] for r in body["recommendations"]] == ["Good"]
    assert body["result_count"] == 1


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------
def test_pruning_keeps_the_cap_per_user_per_group(client, club, fake_claude, monkeypatch):
    """Rows beyond the retained cap are deleted, newest kept."""
    monkeypatch.setattr(recommend_routes, "RECENT_SEARCHES_RETAINED", 3)
    fake_claude(*[claude_response(FIVE) for _ in range(6)])
    for i in range(6):
        _ask(client, club["group_id"], [club["owner_id"]], prompt=f"Ask number {i}")

    rows = _fetchall("SELECT prompt FROM recent_searches ORDER BY id")
    assert len(rows) == 3
    assert [r[0] for r in rows] == ["Ask number 3", "Ask number 4", "Ask number 5"]


def test_pruning_does_not_touch_another_members_history(client, club, fake_claude, monkeypatch):
    """The prune is bucket-local: one member's overflow never deletes another's rows."""
    monkeypatch.setattr(recommend_routes, "RECENT_SEARCHES_RETAINED", 2)
    fake_claude(*[claude_response(FIVE) for _ in range(5)])

    _login_reader(client)
    _ask(client, club["group_id"], [club["member_id"]], prompt="Reader keeps this")

    client.post("/api/logout")
    client.post("/api/login", json={"email": "owner@example.com", "password": OWNER_PASSWORD})
    for i in range(4):
        _ask(client, club["group_id"], [club["owner_id"]], prompt=f"Owner ask {i}")

    reader_rows = _fetchall(
        "SELECT prompt FROM recent_searches WHERE user_id = ?", (club["member_id"],)
    )
    owner_rows = _fetchall(
        "SELECT prompt FROM recent_searches WHERE user_id = ?", (club["owner_id"],)
    )
    assert [r[0] for r in reader_rows] == ["Reader keeps this"]
    assert len(owner_rows) == 2


# ---------------------------------------------------------------------------
# Corrupt stored JSON degrades rather than 500s
# ---------------------------------------------------------------------------
def _corrupt_watching(value):
    """Overwrite the stored `watching` value on every recent_searches row."""

    async def go():
        async with db.connect() as conn:
            await conn.execute("UPDATE recent_searches SET watching = ?", (value,))
            await conn.commit()

    _run(go())


def _corrupt_results(value):
    """Overwrite the stored `results_json` payload on every recent_searches row."""

    async def go():
        async with db.connect() as conn:
            await conn.execute("UPDATE recent_searches SET results_json = ?", (value,))
            await conn.commit()

    _run(go())


def test_unparseable_watching_degrades_to_empty(client, club, fake_claude):
    """A corrupt `watching` value costs that row its reader list, not the whole listing."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    _corrupt_watching("not json at all")

    res = client.get(f"/api/groups/{club['group_id']}/recent-searches")

    assert res.status_code == 200
    assert res.json()["searches"][0]["watching"] == []
    assert res.json()["searches"][0]["prompt"] == "Something twisty and cold"


def test_unparseable_stored_results_replay_as_empty(client, club, fake_claude):
    """A corrupt payload is reported as an empty saved set, never a 500."""
    fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    search_id = client.get(f"/api/groups/{club['group_id']}/recent-searches").json()[
        "searches"
    ][0]["id"]
    _corrupt_results("{ broken")
    _corrupt_watching("also broken")

    res = client.get(f"/api/groups/{club['group_id']}/recent-searches/{search_id}")

    assert res.status_code == 200
    assert res.json()["recommendations"] == []
    assert res.json()["result_count"] == 0
    assert res.json()["watching"] == []
