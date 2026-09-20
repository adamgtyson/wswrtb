"""Group book recommendations (Session 3).

`POST /api/groups/{group_id}/recommend` combines the stored preference profiles of the
selected members with a free-text prompt, asks Claude for exactly five books, and returns
them as validated JSON.

Three things are enforced server-side rather than merely asked for in the prompt, because
a model's compliance is not an authorization or correctness guarantee:

  * Membership — the caller must belong to the group (require_membership), and every
    selected member id must belong to that same group (checked against the DB, 400
    otherwise). Profiles cannot be pulled across tenants by guessing user ids.
  * Duplicates — results are deduped on a normalized (title, author) key.
  * Named titles/authors — anything the member named in their own prompt is filtered out
    of the results even if Claude ignores the instruction not to recommend it.
  * Existence (Session 4) — every surviving candidate is resolved against Google Books
    and dropped if it cannot be confidently matched. A model producing a plausible
    string is not evidence the book exists; the metadata that comes back with a match is
    what the client renders.

Every Claude call (including the single retry) goes through services/claude_service.py,
which meters cost and rate limits and logs usage. Nothing here touches the SDK. Likewise
every Google Books call goes through services/google_books.py.

Session 5 adds recent searches: every successful response is stored whole, so a member can
re-open a past result set without spending another Claude call. The replay endpoints below
read that stored payload and never call an API — that is the entire point of the feature.
"""
import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app import db
from app.deps import require_membership
from app.models import (
    RecentSearchDetail,
    RecentSearchListResponse,
    RecentSearchSummary,
    Recommendation,
    RecommendationResponse,
    RecommendRequest,
    VerifiedRecommendation,
)
from app.services import claude_service, google_books

logger = logging.getLogger(__name__)

router = APIRouter()

_STATIC = Path(__file__).parent.parent.parent / "static"

# How many books to ask for and return. Matches the candidate count locked for the
# Session 6 ranked-voting round, so a recommendation set can become a ballot unchanged.
RECOMMENDATION_COUNT = 5

# At most one retry when dedup/filtering leaves us short — never an unbounded loop.
MAX_RETRY_CALLS = 1

# Label recorded on each ai_usage row for this route.
ENDPOINT_LABEL = "recommend"

# How many past searches a member can see for a group.
RECENT_SEARCHES_LIMIT = 10

# How many rows are actually kept per (member, group) before the oldest are pruned. Kept
# above the display limit so the listing stays full even as rows age out, and so the cap
# is not re-tripped by every single insert.
RECENT_SEARCHES_RETAINED = RECENT_SEARCHES_LIMIT * 2

# Titles/authors shorter than this are not used for the "named in the prompt" filter:
# very short strings ("It", "Us") appear inside unrelated prose and would drop valid books.
MIN_MATCH_LEN = 4

# Leading/trailing markdown code fences, with or without a language tag.
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*|\s*```\s*$")

# Collapse runs of whitespace when normalizing for comparison.
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Page route
# ---------------------------------------------------------------------------
@router.get("/recommend", include_in_schema=False)
async def recommend_page() -> FileResponse:
    """Serve the (placeholder) ask page. Its JS redirects to /login on a 401, so no
    server-side gate is needed on the page itself. Session 5 replaces this UI."""
    return FileResponse(str(_STATIC / "recommend.html"))


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
def _describe_member(profile: dict) -> str:
    """Render one member's stored preferences as a compact prompt block.

    Only preference fields are included — never email or any other identifier. Empty
    fields are omitted rather than sent as "none", which reads as a constraint to a model.
    """
    lines = [f"Reader: {profile['display_name']}"]
    if profile["favorite_genres"]:
        lines.append(f"  Favourite genres: {', '.join(profile['favorite_genres'])}")
    if profile["favorite_authors"]:
        lines.append(f"  Favourite authors: {', '.join(profile['favorite_authors'])}")
    if profile["examples"]:
        lines.append(f"  Books they loved: {', '.join(profile['examples'])}")
    if profile["dislikes"]:
        lines.append(f"  Dislikes (avoid these): {', '.join(profile['dislikes'])}")

    prefs = profile["content_preferences"] or {}
    if prefs:
        bits = []
        if prefs.get("max_violence"):
            bits.append(f"violence at most {prefs['max_violence']}")
        if prefs.get("max_language"):
            bits.append(f"strong language at most {prefs['max_language']}")
        bits.append("romance is fine" if prefs.get("romance_ok") else "no romance")
        if not prefs.get("explicit_ok", False):
            bits.append("nothing sexually explicit")
        lines.append(f"  Content limits: {'; '.join(bits)}")

    if profile["reading_pace"]:
        lines.append(f"  Reading pace: {profile['reading_pace']}")
    if profile["preferred_length"]:
        lines.append(f"  Preferred book length: {profile['preferred_length']}")
    return "\n".join(lines)


def _build_system_prompt(profiles: list[dict], exclusions: list[str] | None = None) -> str:
    """Build the system prompt: reader profiles, the output contract, and hard rules.

    NOTE: member display names and free-text preferences are user-supplied and are
    interpolated here. That is an inherent prompt-injection surface for any AI feature
    built on user data; it is bounded in this app because membership is invite-gated and
    the model's output is schema-validated and filtered server-side before use.
    """
    reader_blocks = "\n\n".join(_describe_member(p) for p in profiles)

    rules = [
        f"Recommend exactly {RECOMMENDATION_COUNT} books that suit ALL of the readers above.",
        "Every book must be real and published; never invent titles or authors.",
        "If the reader's request names a genre, that genre is a HARD CONSTRAINT: every "
        "recommendation must be in it. Do not substitute an adjacent genre.",
        "Never recommend a book or an author that the reader explicitly named in their "
        "request — they already know about those; suggest something new.",
        "Respect every reader's dislikes and content limits.",
        "Each 'reason' is one or two sentences explaining why this group specifically "
        "would enjoy it.",
    ]
    if exclusions:
        rules.append(
            "Do NOT recommend any of these already-suggested titles: "
            + "; ".join(exclusions)
            + ". Suggest different books."
        )

    numbered_rules = "\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, start=1))

    return (
        "You are a book recommender for a group of readers (a book club or family). "
        "You are given each reader's stored preferences and a request from one of them.\n\n"
        f"READERS:\n{reader_blocks}\n\n"
        f"RULES:\n{numbered_rules}\n\n"
        "OUTPUT FORMAT — this is strict:\n"
        "Respond with a JSON array and nothing else. No prose, no markdown, no code "
        "fences, no explanation before or after. The array contains exactly "
        f"{RECOMMENDATION_COUNT} objects with these keys:\n"
        '[{"title": "string", "author": "string", "year": 1965, '
        '"reason": "string"}]\n'
        'Use the publication year as an integer, or null if you are unsure.'
    )


# ---------------------------------------------------------------------------
# Response parsing / filtering
# ---------------------------------------------------------------------------
def strip_code_fences(text: str) -> str:
    """Remove surrounding markdown code fences from a model response.

    Models wrap JSON in ```json ... ``` even when told not to, so strip it defensively
    before parsing rather than treating an otherwise-valid response as a failure.
    """
    cleaned = text.strip()
    # Two passes: the opening fence (with optional language tag) and the closing fence.
    cleaned = _FENCE_RE.sub("", cleaned)
    cleaned = _FENCE_RE.sub("", cleaned)
    return cleaned.strip()


def parse_recommendations(text: str) -> list[Recommendation]:
    """Parse and validate Claude's response into Recommendation models.

    Accepts either a bare JSON array or an object wrapping one (models occasionally return
    {"recommendations": [...]}). Any malformed or schema-violating response is a service
    failure, not a crash: it raises ClaudeServiceError, which the route maps to a clean 502.
    """
    cleaned = strip_code_fences(text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Claude returned unparseable JSON: %s", cleaned[:500])
        raise claude_service.ClaudeServiceError("Malformed recommendation response.") from exc

    if isinstance(payload, dict):
        # Tolerate a single wrapping key whose value is the array.
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) != 1:
            logger.error("Claude returned an unexpected object shape: %s", cleaned[:500])
            raise claude_service.ClaudeServiceError("Malformed recommendation response.")
        payload = lists[0]

    if not isinstance(payload, list):
        logger.error("Claude response was not a JSON array: %s", cleaned[:500])
        raise claude_service.ClaudeServiceError("Malformed recommendation response.")

    recommendations: list[Recommendation] = []
    for item in payload:
        try:
            recommendations.append(Recommendation.model_validate(item))
        except (ValidationError, TypeError):
            # Drop the individual malformed entry; a partial set still beats a hard failure.
            logger.warning("Dropping malformed recommendation entry: %r", item)

    if not recommendations:
        raise claude_service.ClaudeServiceError("No usable recommendations returned.")
    return recommendations


def _normalize(value: str) -> str:
    """Lowercase and collapse whitespace for case-insensitive comparison."""
    return _WHITESPACE_RE.sub(" ", value.strip().lower())


def _dedupe_key(rec: Recommendation) -> tuple[str, str]:
    """Identity of a recommendation for dedup: normalized (title, author)."""
    return (_normalize(rec.title), _normalize(rec.author))


def named_in_prompt(rec: Recommendation, normalized_prompt: str) -> bool:
    """Return True if the member's own prompt names this book's title or author.

    Server-side safety net for the "never recommend what they already named" rule — the
    same discipline used elsewhere in this project: constraints are enforced in code, not
    only prompt-engineered. Very short titles/authors are skipped (see MIN_MATCH_LEN).
    """
    for field in (rec.title, rec.author):
        candidate = _normalize(field)
        if len(candidate) >= MIN_MATCH_LEN and candidate in normalized_prompt:
            return True
    return False


# ---------------------------------------------------------------------------
# Google Books verification
# ---------------------------------------------------------------------------
def _merge_metadata(rec: Recommendation, metadata: dict | None) -> VerifiedRecommendation:
    """Build the response model for one book: Claude's fields plus verified metadata.

    With `metadata` None the book is returned UNVERIFIED (verified=False, no Google
    fields) — reached only when Google Books itself was unreachable. Claude's title and
    author stay the displayed values either way; the canonical ones ride alongside so
    Session 5's cards can prefer them without this route rewriting what the model said.
    """
    if metadata is None:
        return VerifiedRecommendation(**rec.model_dump())
    return VerifiedRecommendation(**rec.model_dump(), verified=True, **metadata)


async def _verify(rec: Recommendation) -> VerifiedRecommendation | None:
    """Resolve one candidate against Google Books (cache-first).

    Three outcomes, mirroring the service's three:
      * enriched VerifiedRecommendation — confidently matched a real volume.
      * None — Google answered and nothing matched. The book is probably invented; the
        caller drops it and the existing single retry asks for a replacement.
      * unverified VerifiedRecommendation — Google Books was DOWN. An outage is not
        evidence a book is fake, so the request degrades rather than silently emptying
        the member's results. Logged at warning level, because it means someone may be
        looking at a book nothing has checked.
    """
    try:
        metadata = await google_books.lookup(rec.title, rec.author)
    except google_books.GoogleBooksUnavailable as exc:
        logger.warning(
            "Google Books unavailable (%s) — returning '%s' by %s UNVERIFIED",
            exc,
            rec.title,
            rec.author,
        )
        return _merge_metadata(rec, None)

    if metadata is None:
        logger.info(
            "Dropping '%s' by %s — no confident Google Books match", rec.title, rec.author
        )
        return None
    return _merge_metadata(rec, metadata)


async def _accept(
    candidates: list[Recommendation],
    accepted: list[VerifiedRecommendation],
    seen_keys: set,
    seen_titles: list[str],
    normalized_prompt: str,
) -> None:
    """Fold `candidates` into `accepted`: dedup, prompt-filter, then verify.

    Mutates `accepted`, `seen_keys`, and `seen_titles` in place. Every candidate title is
    recorded in `seen_titles` — including ones dropped as duplicates, as prompt-named, or
    as unverifiable — so the retry call excludes everything already tried, not just what
    survived. That is why short-by-dedup and short-by-unverified need no separate retry
    paths: both simply leave `accepted` short of the target.

    Verification runs concurrently: each unseen candidate can cost an HTTP round trip, and
    five of those in series would add seconds to a request that already waited on Claude.
    `gather` preserves order, so the result order is still Claude's.
    """
    survivors: list[Recommendation] = []
    for rec in candidates:
        seen_titles.append(rec.title)
        key = _dedupe_key(rec)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if named_in_prompt(rec, normalized_prompt):
            logger.info("Filtered '%s' — named in the member's own prompt", rec.title)
            continue
        survivors.append(rec)

    if not survivors:
        return
    verified = await asyncio.gather(*(_verify(rec) for rec in survivors))
    accepted.extend(rec for rec in verified if rec is not None)


# ---------------------------------------------------------------------------
# Search history
# ---------------------------------------------------------------------------
async def _record_history(
    *,
    user_id: int,
    group_id: int,
    prompt: str,
    profiles: list[dict],
    results: list[VerifiedRecommendation],
) -> None:
    """Store this search and its results. NEVER raises.

    Same philosophy as the Session 4 api_cache write: history is an optimization, not a
    dependency. The member has already been charged an AI call by the time this runs, so
    a failure to persist their history must not turn a successful, billed recommendation
    into a 500. Logged at warning level and swallowed.

    `watching` holds the selected members' display names (the column name is inherited
    verbatim from the sibling movie app; here it is who was reading). Both it and the
    result set are serialized in Python and bound as parameters, never interpolated.
    """
    try:
        await db.record_recent_search(
            user_id=user_id,
            group_id=group_id,
            prompt=prompt,
            result_count=len(results),
            watching=[p["display_name"] for p in profiles],
            results=[rec.model_dump() for rec in results],
            keep=RECENT_SEARCHES_RETAINED,
        )
    except Exception as exc:  # deliberately broad — nothing here may fail the request
        logger.warning("Failed to record search history for user %s: %s", user_id, exc)


def _revalidate_stored(rows: list) -> list[VerifiedRecommendation]:
    """Re-validate stored results through the response schema on the way out.

    A stored payload was written by whatever version of the app was running that day, so
    it is treated like any other untrusted input rather than trusted because we wrote it.
    An entry that no longer fits the schema is dropped with a warning — a partial replay
    beats a 500 on history a member cannot re-run without paying for it again.
    """
    recommendations: list[VerifiedRecommendation] = []
    for item in rows:
        try:
            recommendations.append(VerifiedRecommendation.model_validate(item))
        except (ValidationError, TypeError):
            logger.warning("Dropping unusable stored recommendation: %r", item)
    return recommendations


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@router.post(
    "/api/groups/{group_id}/recommend",
    summary="Get book recommendations for selected group members",
    description=(
        "Combines the stored preference profiles of the selected members with a free-text "
        "prompt and returns up to five book recommendations, each verified against Google "
        "Books and enriched with its real metadata (id, cover, description, page count). "
        "Every selected member must belong to the group. Metered: the call counts against "
        "the caller's AI rate limits and the global daily cost ceiling. Books that cannot "
        "be verified are dropped, so fewer than five may come back."
    ),
    response_model=RecommendationResponse,
    tags=["recommendations"],
)
async def recommend_books(
    body: RecommendRequest, ctx: dict = Depends(require_membership())
) -> RecommendationResponse:
    """Ask Claude for `RECOMMENDATION_COUNT` books for the selected members.

    Runs one metered Claude call, parses and validates the response, dedupes it, filters
    out anything the member named in their own prompt, and verifies what is left against
    Google Books. If that leaves fewer than the target count — for any of those reasons —
    makes exactly ONE more call with the already-seen titles as an exclusion list and
    returns whatever survives. A still-short set is returned as a partial rather than
    discarding calls the user has already been charged for.

    Returns 400 if a selected user is not a member of this group, 429 if a cost/rate limit
    refused the call, 502 if Claude failed or returned an unusable response. A Google
    Books outage is not an error here: those books come back with `verified: false`.
    """
    group_id = ctx["group_id"]
    user_id = ctx["user"]["id"]

    profiles = await db.get_group_member_profiles(group_id, body.member_user_ids)
    found_ids = {p["user_id"] for p in profiles}
    missing = [uid for uid in body.member_user_ids if uid not in found_ids]
    if missing:
        # Generic message: don't confirm whether the id exists elsewhere in the system.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One or more selected readers are not members of this group.",
        )

    normalized_prompt = _normalize(body.prompt)
    accepted: list[VerifiedRecommendation] = []
    seen_keys: set = set()
    seen_titles: list[str] = []

    raw = await claude_service.complete_text(
        user_id=user_id,
        group_id=group_id,
        endpoint=ENDPOINT_LABEL,
        system_prompt=_build_system_prompt(profiles),
        user_prompt=body.prompt,
    )
    await _accept(
        parse_recommendations(raw), accepted, seen_keys, seen_titles, normalized_prompt
    )

    # One top-up attempt only — bounded retry, never a loop.
    for _ in range(MAX_RETRY_CALLS):
        if len(accepted) >= RECOMMENDATION_COUNT:
            break
        logger.info(
            "Only %s of %s recommendations survived dedup/filtering/verification "
            "— retrying once",
            len(accepted),
            RECOMMENDATION_COUNT,
        )
        try:
            raw = await claude_service.complete_text(
                user_id=user_id,
                group_id=group_id,
                endpoint=ENDPOINT_LABEL,
                system_prompt=_build_system_prompt(profiles, exclusions=seen_titles),
                user_prompt=body.prompt,
            )
            await _accept(
                parse_recommendations(raw), accepted, seen_keys, seen_titles, normalized_prompt
            )
        except (claude_service.AILimitError, claude_service.ClaudeServiceError) as exc:
            # The top-up is best-effort. If the retry trips a limit or comes back
            # unusable, return the books we already have rather than throwing away a
            # call the user has already been charged for.
            logger.info("Recommendation top-up call failed (%s) — returning partial set", exc)
            break

    final = accepted[:RECOMMENDATION_COUNT]
    # Best-effort and last: the response is already earned at this point.
    await _record_history(
        user_id=user_id,
        group_id=group_id,
        prompt=body.prompt,
        profiles=profiles,
        results=final,
    )
    return RecommendationResponse(
        group_id=group_id, count=len(final), recommendations=final
    )


@router.get(
    "/api/groups/{group_id}/recent-searches",
    summary="List the caller's own recent searches in this group",
    description=(
        "Returns the calling member's most recent searches for this group, newest first. "
        "Only the caller's own searches are ever returned — history is private to the "
        "member who ran it, even among members of the same group. The stored results are "
        "not included; fetch one search by id to replay it."
    ),
    response_model=RecentSearchListResponse,
    tags=["recommendations"],
)
async def list_recent_searches(
    ctx: dict = Depends(require_membership()),
) -> RecentSearchListResponse:
    """Return up to RECENT_SEARCHES_LIMIT of the caller's searches in this group."""
    group_id = ctx["group_id"]
    rows = await db.list_recent_searches(
        ctx["user"]["id"], group_id, RECENT_SEARCHES_LIMIT
    )
    searches = [RecentSearchSummary(**row) for row in rows]
    return RecentSearchListResponse(
        group_id=group_id, count=len(searches), searches=searches
    )


@router.get(
    "/api/groups/{group_id}/recent-searches/{search_id}",
    summary="Replay one stored search",
    description=(
        "Returns the books a past search produced, exactly as they were stored. This "
        "spends NO AI call and makes no external request — replaying history is the "
        "feature. The results are therefore as old as `created_at`, which clients must "
        "display: a saved set looks identical to a fresh one otherwise. Returns 404 for "
        "a search that is not the caller's own in this group."
    ),
    response_model=RecentSearchDetail,
    tags=["recommendations"],
)
async def replay_recent_search(
    search_id: int, ctx: dict = Depends(require_membership())
) -> RecentSearchDetail:
    """Return one stored search's results, re-validated, without spending anything.

    The owning user and group are part of the lookup rather than checked afterwards, so
    another member's search — or the caller's own search in a different group — is simply
    not found. 404 rather than 403: the caller learns nothing about whether that id exists.
    """
    group_id = ctx["group_id"]
    row = await db.get_recent_search(search_id, ctx["user"]["id"], group_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Search not found."
        )

    recommendations = _revalidate_stored(row["results"])
    return RecentSearchDetail(
        id=row["id"],
        group_id=group_id,
        prompt=row["prompt"],
        # The count of what is actually being returned, not the stored figure — they
        # differ only if a stored entry failed re-validation, and the client's badge must
        # match the cards it renders.
        result_count=len(recommendations),
        watching=row["watching"],
        created_at=row["created_at"],
        recommendations=recommendations,
    )
