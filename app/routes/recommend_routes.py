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

Every Claude call (including the single retry) goes through services/claude_service.py,
which meters cost and rate limits and logs usage. Nothing here touches the SDK.
"""
import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app import db
from app.deps import require_membership
from app.models import Recommendation, RecommendationResponse, RecommendRequest
from app.services import claude_service

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


def _accept(
    candidates: list[Recommendation],
    accepted: list[Recommendation],
    seen_keys: set,
    seen_titles: list[str],
    normalized_prompt: str,
) -> None:
    """Fold `candidates` into `accepted`, dropping duplicates and prompt-named books.

    Mutates `accepted`, `seen_keys`, and `seen_titles` in place. Every candidate title is
    recorded in `seen_titles` — including rejected ones — so the retry call can exclude
    everything already suggested, not just what survived.
    """
    for rec in candidates:
        seen_titles.append(rec.title)
        key = _dedupe_key(rec)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if named_in_prompt(rec, normalized_prompt):
            logger.info("Filtered '%s' — named in the member's own prompt", rec.title)
            continue
        accepted.append(rec)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
@router.post(
    "/api/groups/{group_id}/recommend",
    summary="Get book recommendations for selected group members",
    description=(
        "Combines the stored preference profiles of the selected members with a free-text "
        "prompt and returns up to five validated book recommendations. Every selected "
        "member must belong to the group. Metered: the call counts against the caller's "
        "AI rate limits and the global daily cost ceiling."
    ),
    response_model=RecommendationResponse,
    tags=["recommendations"],
)
async def recommend_books(
    body: RecommendRequest, ctx: dict = Depends(require_membership())
) -> RecommendationResponse:
    """Ask Claude for `RECOMMENDATION_COUNT` books for the selected members.

    Runs one metered Claude call, parses and validates the response, dedupes it, and
    filters out anything the member named in their own prompt. If that leaves fewer than
    the target count, makes exactly ONE more call with the already-seen titles as an
    exclusion list and returns whatever survives.

    Returns 400 if a selected user is not a member of this group, 429 if a cost/rate limit
    refused the call, 502 if Claude failed or returned an unusable response.
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
    accepted: list[Recommendation] = []
    seen_keys: set = set()
    seen_titles: list[str] = []

    raw = await claude_service.complete_text(
        user_id=user_id,
        group_id=group_id,
        endpoint=ENDPOINT_LABEL,
        system_prompt=_build_system_prompt(profiles),
        user_prompt=body.prompt,
    )
    _accept(parse_recommendations(raw), accepted, seen_keys, seen_titles, normalized_prompt)

    # One top-up attempt only — bounded retry, never a loop.
    for _ in range(MAX_RETRY_CALLS):
        if len(accepted) >= RECOMMENDATION_COUNT:
            break
        logger.info(
            "Only %s of %s recommendations survived dedup/filtering — retrying once",
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
            _accept(
                parse_recommendations(raw), accepted, seen_keys, seen_titles, normalized_prompt
            )
        except (claude_service.AILimitError, claude_service.ClaudeServiceError) as exc:
            # The top-up is best-effort. If the retry trips a limit or comes back
            # unusable, return the books we already have rather than throwing away a
            # call the user has already been charged for.
            logger.info("Recommendation top-up call failed (%s) — returning partial set", exc)
            break

    final = accepted[:RECOMMENDATION_COUNT]
    return RecommendationResponse(
        group_id=group_id, count=len(final), recommendations=final
    )
