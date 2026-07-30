"""Recommendation endpoint: authorization, response parsing, dedup/retry, and metering.

Every test mocks the Anthropic client — no real API call is ever made. The mocked client
also records what was sent, so the tests can assert on the constructed prompt (e.g. that
the retry carries an exclusion list).
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
    """A logged-in owner with one other member. Returns ids and the test client."""
    info = seed(code="RECCLUB", seats=8)
    register(client, code="RECCLUB", email="reader@example.com", display_name="Reader")
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_happy_path(client, club, fake_claude):
    """Five books come back with all fields populated, from a single API call."""
    fake = fake_claude(claude_response(FIVE))
    res = _ask(client, club["group_id"], [club["owner_id"], club["member_id"]])

    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 5
    assert len(body["recommendations"]) == 5
    first = body["recommendations"][0]
    assert first["title"] == "Alpha"
    assert first["author"] == "Author of Alpha"
    assert first["reason"]
    assert fake.call_count == 1


def test_selected_member_profiles_are_in_the_prompt(client, club, fake_claude):
    """The system prompt is built from the selected members' stored profile columns."""
    client.put(
        "/api/profile",
        json={
            "favorite_genres": ["Nordic noir"],
            "favorite_authors": ["Jo Nesbo"],
            "examples": [],
            "dislikes": ["Gore"],
            "content_preferences": {},
            "reading_pace": "slow",
            "preferred_length": "short",
        },
    )
    fake = fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])

    system = fake.calls[0]["system"]
    assert "Nordic noir" in system
    assert "Jo Nesbo" in system
    assert "Gore" in system
    assert "slow" in system
    # Emails must never reach the prompt.
    assert "owner@example.com" not in system


def test_prompt_states_the_hard_constraints(client, club, fake_claude):
    """Genre-as-hard-constraint and the don't-repeat-my-titles rule are both instructed."""
    fake = fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    system = fake.calls[0]["system"]
    assert "HARD CONSTRAINT" in system
    assert "Never recommend a book or an author that the reader explicitly named" in system


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def test_non_member_caller_is_forbidden(client, seed, fake_claude):
    """require_membership still gates the route: a non-member gets 403, no API call."""
    seed(code="GROUPA", seats=5)
    other = seed(code="GROUPB", seats=5, email="outsider@example.com", group_name="Other Club")
    register(client, code="GROUPA", email="insider@example.com")
    client.post("/api/login", json={"email": "insider@example.com", "password": "password1"})

    fake = fake_claude()
    res = _ask(client, other["group_id"], [other["owner_user_id"]])
    assert res.status_code == 403
    assert fake.call_count == 0


def test_cross_tenant_member_selection_is_rejected(client, club, seed, fake_claude, fetchone):
    """A user id from another group cannot be smuggled into member_user_ids."""
    outsider = seed(
        code="OTHERCLUB", seats=5, email="stranger@example.com", group_name="Stranger Club"
    )
    fake = fake_claude()

    res = _ask(client, club["group_id"], [club["owner_id"], outsider["owner_user_id"]])
    assert res.status_code == 400
    assert "not members of this group" in res.json()["detail"]
    # No Claude call, so no spend and no profile leak.
    assert fake.call_count == 0
    assert fetchone("SELECT COUNT(*) FROM ai_usage")[0] == 0


def test_unknown_user_id_is_rejected(client, club, fake_claude):
    """A user id that doesn't exist at all is refused the same generic way."""
    fake = fake_claude()
    res = _ask(client, club["group_id"], [club["owner_id"], 999999])
    assert res.status_code == 400
    assert fake.call_count == 0


def test_login_required(client, seed):
    """An unauthenticated request gets a 401, not a recommendation."""
    info = seed(code="ANONCLUB", seats=5)
    res = _ask(client, info["group_id"], [info["owner_user_id"]])
    assert res.status_code == 401


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def test_blank_prompt_rejected(client, club, fake_claude):
    """An empty prompt fails Pydantic validation before any API call."""
    fake = fake_claude()
    res = _ask(client, club["group_id"], [club["owner_id"]], prompt="   ")
    assert res.status_code == 422
    assert fake.call_count == 0


def test_overlong_prompt_rejected(client, club, fake_claude):
    """The prompt length cap is enforced server-side."""
    fake = fake_claude()
    too_long = "x" * (claude_service.MAX_PROMPT_CHARS + 1)
    res = _ask(client, club["group_id"], [club["owner_id"]], prompt=too_long)
    assert res.status_code == 422
    assert fake.call_count == 0


def test_empty_member_list_rejected(client, club, fake_claude):
    """At least one reader must be selected."""
    fake = fake_claude()
    res = _ask(client, club["group_id"], [])
    assert res.status_code == 422
    assert fake.call_count == 0


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def test_markdown_fences_are_stripped():
    """```json fenced output parses fine — models add fences even when told not to."""
    fenced = "```json\n" + FIVE + "\n```"
    parsed = recommend_routes.parse_recommendations(fenced)
    assert len(parsed) == 5
    assert parsed[0].title == "Alpha"


def test_bare_fences_are_stripped():
    """A fence with no language tag is handled too."""
    parsed = recommend_routes.parse_recommendations("```\n" + FIVE + "\n```")
    assert len(parsed) == 5


def test_object_wrapper_is_unwrapped():
    """{"recommendations": [...]} is tolerated rather than treated as a failure."""
    parsed = recommend_routes.parse_recommendations(
        json.dumps({"recommendations": json.loads(FIVE)})
    )
    assert len(parsed) == 5


def test_fenced_response_works_end_to_end(client, club, fake_claude):
    """Fence stripping is wired into the route, not just the helper."""
    fake_claude(claude_response("```json\n" + FIVE + "\n```"))
    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 200
    assert res.json()["count"] == 5


def test_ambiguous_object_shape_is_a_service_error():
    """An object with several arrays is unusable — refuse rather than guess which is right."""
    payload = {"books": json.loads(FIVE), "alternatives": json.loads(FIVE)}
    with pytest.raises(claude_service.ClaudeServiceError):
        recommend_routes.parse_recommendations(json.dumps(payload))


def test_non_array_json_is_a_service_error():
    """Valid JSON that isn't a list of books is still a failure."""
    with pytest.raises(claude_service.ClaudeServiceError):
        recommend_routes.parse_recommendations('"just a string"')


def test_all_entries_malformed_is_a_service_error():
    """If nothing in the array validates, there is no usable result to return."""
    with pytest.raises(claude_service.ClaudeServiceError):
        recommend_routes.parse_recommendations('[{"nope": 1}, {"also": "nope"}]')


def test_recommend_page_is_served(client):
    """The placeholder ask page renders; its JS handles the auth bounce."""
    res = client.get("/recommend")
    assert res.status_code == 200
    assert "Ask for a book" in res.text


def test_malformed_json_is_a_502(client, club, fake_claude):
    """Unparseable output is a clean service error, never a stack trace."""
    fake_claude(claude_response("I'm afraid I can't do that."))
    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 502
    assert "detail" in res.json()


def test_malformed_entries_are_dropped_not_fatal(client, club, fake_claude):
    """One bad entry in an otherwise valid array doesn't sink the whole response."""
    payload = json.loads(FIVE)
    payload[2] = {"title": "No author field"}  # missing required keys
    fake_claude(claude_response(json.dumps(payload)), claude_response(_books("Foxtrot")))
    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 200
    titles = [r["title"] for r in res.json()["recommendations"]]
    assert "No author field" not in titles


def test_year_variants_are_coerced():
    """A year given as a string, or as junk, doesn't fail validation."""
    body = json.dumps(
        [
            {"title": "A", "author": "B", "year": "1965", "reason": "r"},
            {"title": "C", "author": "D", "year": "unknown", "reason": "r"},
            {"title": "E", "author": "F", "year": None, "reason": "r"},
        ]
    )
    parsed = recommend_routes.parse_recommendations(body)
    assert parsed[0].year == 1965
    assert parsed[1].year is None
    assert parsed[2].year is None


# ---------------------------------------------------------------------------
# Dedup + bounded retry
# ---------------------------------------------------------------------------
def test_duplicates_trigger_one_retry_with_exclusions(client, club, fake_claude):
    """Dupes are dropped; ONE retry tops the set back up and carries an exclusion list."""
    first = _books("Alpha", "Alpha", "Bravo", "bravo", "Charlie")  # 3 unique
    second = _books("Delta", "Echo")
    fake = fake_claude(claude_response(first), claude_response(second))

    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 200
    titles = [r["title"] for r in res.json()["recommendations"]]
    assert titles == ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    assert fake.call_count == 2

    retry_system = fake.calls[1]["system"]
    assert "Do NOT recommend any of these already-suggested titles" in retry_system
    for seen in ("Alpha", "Bravo", "Charlie"):
        assert seen in retry_system


def test_dedup_is_case_insensitive_on_title_and_author(client, club, fake_claude):
    """Normalized (title, author) is the identity — casing and spacing don't defeat it."""
    body = json.dumps(
        [
            {"title": "Alpha", "author": "Ann Lee", "year": 2001, "reason": "r"},
            {"title": "  alpha ", "author": "ANN  LEE", "year": 2001, "reason": "r"},
        ]
    )
    fake = fake_claude(claude_response(body), claude_response(_books("Bravo")))
    res = _ask(client, club["group_id"], [club["owner_id"]])
    titles = [r["title"] for r in res.json()["recommendations"]]
    assert titles == ["Alpha", "Bravo"]
    assert fake.call_count == 2


def test_retry_happens_at_most_once(client, club, fake_claude):
    """Still short after the retry? Return what's left — never loop."""
    dupes = _books("Alpha", "Alpha", "Alpha")
    fake = fake_claude(claude_response(dupes), claude_response(dupes))

    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert fake.call_count == 2  # the fake asserts loudly on a third call


def test_partial_set_returned_when_the_retry_fails(client, club, fake_claude):
    """A failed top-up returns the books already paid for, not a 502."""
    fake_claude(claude_response(_books("Alpha", "Bravo")), claude_response("not json at all"))
    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 200
    assert [r["title"] for r in res.json()["recommendations"]] == ["Alpha", "Bravo"]


def test_partial_set_returned_when_the_retry_is_rate_limited(
    client, club, fake_claude, monkeypatch
):
    """Tripping a limit on the top-up call returns the partial set rather than a 429."""
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 1)
    fake = fake_claude(claude_response(_books("Alpha", "Bravo")))
    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 200
    assert res.json()["count"] == 2
    assert fake.call_count == 1  # the retry never reached the API


def test_no_retry_when_the_first_call_is_complete(client, club, fake_claude):
    """A full set first time means exactly one API call — no speculative retry."""
    fake = fake_claude(claude_response(FIVE))
    _ask(client, club["group_id"], [club["owner_id"]])
    assert fake.call_count == 1


# ---------------------------------------------------------------------------
# Server-side filter for titles named in the member's own prompt
# ---------------------------------------------------------------------------
def test_title_named_in_the_prompt_is_filtered_out(client, club, fake_claude):
    """The 'don't suggest what I already named' rule is enforced in code, not just asked."""
    body = _books("Project Hail Mary", "Bravo", "Charlie", "Delta", "Echo")
    fake = fake_claude(claude_response(body), claude_response(_books("Foxtrot")))

    res = _ask(
        client,
        club["group_id"],
        [club["owner_id"]],
        prompt="I loved Project Hail Mary, what next?",
    )
    titles = [r["title"] for r in res.json()["recommendations"]]
    assert "Project Hail Mary" not in titles
    assert "Foxtrot" in titles


def test_author_named_in_the_prompt_is_filtered_out(client, club, fake_claude):
    """Authors the member named are filtered too, not only titles."""
    body = json.dumps(
        [
            {"title": "Some Book", "author": "Brandon Sanderson", "year": 2010, "reason": "r"},
            {"title": "Other Book", "author": "Someone Else", "year": 2011, "reason": "r"},
        ]
    )
    fake_claude(claude_response(body), claude_response(_books("Foxtrot")))
    res = _ask(
        client,
        club["group_id"],
        [club["owner_id"]],
        prompt="Read everything by Brandon Sanderson already, need something new",
    )
    titles = [r["title"] for r in res.json()["recommendations"]]
    assert "Some Book" not in titles
    assert "Other Book" in titles


def test_very_short_titles_are_not_filtered():
    """Short titles are exempt from the substring filter so valid books aren't dropped."""
    from app.models import Recommendation

    rec = Recommendation(title="It", author="Stephen King", year=1986, reason="r")
    # "it" appears in this prompt, but the title is below MIN_MATCH_LEN.
    assert recommend_routes.named_in_prompt(rec, "i want it to be scary") is False


# ---------------------------------------------------------------------------
# Metering wired into the route
# ---------------------------------------------------------------------------
def test_usage_is_logged_for_every_call_including_the_retry(client, club, fake_claude, fetchone):
    """Both the initial call and the retry land in ai_usage with their own tokens."""
    fake_claude(
        claude_response(_books("Alpha"), input_tokens=800, output_tokens=100),
        claude_response(_books("Bravo"), input_tokens=900, output_tokens=120),
    )
    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 200

    rows = _run(_all_usage())
    assert len(rows) == 2
    assert [r[0] for r in rows] == [800, 900]
    assert [r[1] for r in rows] == [100, 120]
    assert all(r[2] > 0 for r in rows)  # cost computed
    assert all(r[3] == club["group_id"] for r in rows)  # group recorded
    assert all(r[4] == recommend_routes.ENDPOINT_LABEL for r in rows)


async def _all_usage():
    async with db.connect() as conn:
        async with conn.execute(
            "SELECT input_tokens, output_tokens, est_cost_usd, group_id, endpoint "
            "FROM ai_usage ORDER BY id"
        ) as cur:
            return await cur.fetchall()


def test_hourly_limit_returns_429(client, club, fake_claude, monkeypatch):
    """Once the caller's hourly limit is reached, further requests are 429."""
    monkeypatch.setattr(claude_service, "AI_RATE_PER_HOUR", 2)
    fake = fake_claude(claude_response(FIVE), claude_response(FIVE))

    for _ in range(2):
        assert _ask(client, club["group_id"], [club["owner_id"]]).status_code == 200
    throttled = _ask(client, club["group_id"], [club["owner_id"]])
    assert throttled.status_code == 429
    assert throttled.json()["detail"]
    assert fake.call_count == 2  # the throttled request never reached the API


def test_global_ceiling_returns_429_for_a_different_user(client, club, fake_claude, monkeypatch):
    """One user tripping the global ceiling blocks a different user's request too."""
    monkeypatch.setattr(claude_service, "AI_DAILY_COST_CEILING_USD", 0.01)

    async def burn():
        await db.record_ai_usage(
            user_id=club["member_id"], group_id=club["group_id"], model="test",
            input_tokens=1, output_tokens=1, est_cost_usd=1.0, endpoint="test",
        )

    _run(burn())

    fake = fake_claude()
    res = _ask(client, club["group_id"], [club["owner_id"]])  # owner, not the spender
    assert res.status_code == 429
    assert fake.call_count == 0


def test_config_error_returns_503(client, club, monkeypatch):
    """A missing API key surfaces as a 503, never a silent success or a 500."""
    monkeypatch.setattr(claude_service, "_client", None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = _ask(client, club["group_id"], [club["owner_id"]])
    assert res.status_code == 503
