"""Thumbs up/down feedback on a book (Session 5).

Split out of recommend_routes.py deliberately. Every other route in that module is
group-scoped — it resolves a group id, authorizes membership against it, and spends a
metered AI call. Feedback is none of those things: the `feedback` table carries no
group_id, so a rating is per-user and account-global, authorized by authentication alone.
Folding it in would have put a differently-authorized, differently-shaped resource inside
a module whose docstring is about prompt construction and AI metering.

Store-only this session: nothing here feeds the recommendation prompt. `claude_service`
and `_build_system_prompt` are untouched, so a rating changes what the member sees on a
card and nothing else.

The table's uniqueness key is (user_id, title), which drives the whole design: writes are
upserts on that key, the delete is a toggle rather than a resource removal, and the client
must send the same title the recommendation displayed (Claude's, not the Google Books
canonical one) or it will create a second row for the same book.
"""
import logging

from fastapi import APIRouter, Depends, Query, Response, status

from app import db
from app.auth import get_current_user
from app.models import FeedbackMapResponse, FeedbackRequest, FeedbackResponse
from app.services import rate_limit

logger = logging.getLogger(__name__)

router = APIRouter()

# Ratings are looked up for a whole result set at once. A set is RECOMMENDATION_COUNT
# books today; this leaves room for a replayed set plus a fresh one without letting a
# caller ask about an unbounded list of titles in one query string.
MAX_TITLES_PER_LOOKUP = 50

# Bucket prefix for the write-path limiter. Keyed by user id (not IP): these endpoints are
# authenticated, so the account is the meaningful subject, and a shared household IP must
# not throttle one member because another is rating books.
FEEDBACK_BUCKET_PREFIX = "feedback"


@router.put(
    "/api/feedback",
    summary="Rate a book thumbs up or down",
    description=(
        "Records the calling member's rating for one book. Ratings are per-user and "
        "account-global — they are not scoped to a group. Re-rating the same title "
        "updates the existing row rather than creating a second one, and submitting the "
        "same rating twice is an idempotent no-op. Send the title exactly as the "
        "recommendation displayed it. Rate limited per user."
    ),
    response_model=FeedbackResponse,
    tags=["feedback"],
)
async def rate_book(
    body: FeedbackRequest, user: dict = Depends(get_current_user)
) -> FeedbackResponse:
    """Upsert the current user's rating for one title.

    Any authenticated member may rate any book: there is no group dimension in the
    `feedback` table to authorize against, and a rating exposes nothing about another
    tenant. Returns the stored rating so the client can reconcile its optimistic update.
    """
    await rate_limit.check_and_record(
        f"{FEEDBACK_BUCKET_PREFIX}:{user['id']}",
        rate_limit.FEEDBACK_LIMIT,
        rate_limit.FEEDBACK_WINDOW_SECONDS,
    )
    await db.upsert_feedback(
        user_id=user["id"],
        title=body.title,
        author=body.author,
        google_books_id=body.google_books_id,
        rating=body.rating,
    )
    return FeedbackResponse(title=body.title, rating=body.rating)


@router.delete(
    "/api/feedback",
    summary="Clear a rating",
    description=(
        "Removes the calling member's rating for one title, so the book shows as unrated "
        "again. This is a toggle, not a resource: clearing a rating that was never set "
        "succeeds with 204. Rate limited per user, like the write path."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["feedback"],
)
async def clear_rating(
    title: str = Query(..., min_length=1, max_length=300),
    user: dict = Depends(get_current_user),
) -> Response:
    """Delete the current user's rating for `title`, if any.

    Always 204. A missing row is not a 404 because the client's model of this endpoint is
    "this book is now unrated" — a state the request achieves either way, which also makes
    a double-click on un-toggle harmless.
    """
    await rate_limit.check_and_record(
        f"{FEEDBACK_BUCKET_PREFIX}:{user['id']}",
        rate_limit.FEEDBACK_LIMIT,
        rate_limit.FEEDBACK_WINDOW_SECONDS,
    )
    await db.delete_feedback(user["id"], title.strip())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/api/feedback",
    summary="Look up the caller's existing ratings for a set of titles",
    description=(
        "Returns the calling member's ratings for the given titles as a {title: rating} "
        "map. Only rated titles appear; an absent title means unrated. Repeat the "
        "`titles` parameter once per book so a page of cards costs one request instead of "
        "one per card."
    ),
    response_model=FeedbackMapResponse,
    tags=["feedback"],
)
async def get_ratings(
    titles: list[str] = Query(default_factory=list, max_length=MAX_TITLES_PER_LOOKUP),
    user: dict = Depends(get_current_user),
) -> FeedbackMapResponse:
    """Return {title: rating} for whichever of `titles` the current user has rated.

    Read-only and cheap, so it is not rate limited — the write path is. Blank titles are
    dropped rather than queried; an empty list short-circuits to an empty map without
    touching the database.
    """
    cleaned = [t.strip() for t in titles if t.strip()]
    return FeedbackMapResponse(ratings=await db.get_feedback_for_titles(user["id"], cleaned))
