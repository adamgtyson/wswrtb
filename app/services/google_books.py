"""Google Books lookup and metadata enrichment — the ONLY place that calls Google Books.

Nothing else may hit `googleapis.com`. The single-chokepoint rule here is the same one
that governs `claude_service.py`: one module means one place to add caching, a rate
limit, or a different provider later, and one place to audit when something misbehaves.

What this is for: Claude produces a *string* that looks like a book. That string is not
evidence the book exists — models invent plausible titles. `lookup()` resolves a
(title, author) pair against Google Books and returns real metadata only when the top
results actually match what was asked for, so the route can drop anything unverifiable
before a member ever sees it.

Three outcomes, and callers must handle all three:

  * dict  — a confident match; real metadata for a real book.
  * None  — the API answered, but nothing it returned matched confidently. The book is
            unverified; the caller drops it (see recommend_routes).
  * raise GoogleBooksUnavailable — the API could not be reached at all. A third-party
            outage is NOT the same as "this book doesn't exist", so it gets its own
            signal and the caller degrades instead of dropping or erroring.

Results are cached in the `api_cache` table (never in process memory — see the note on
that table in db.py). `GOOGLE_BOOKS_API_KEY` is optional: the volumes endpoint works
keyless at a lower rate limit, so a missing key lowers headroom but never breaks the app.
"""
import logging
import os
import re

import httpx

from app import db

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Read an integer from the environment, falling back to `default`."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Endpoint + request shape
# ---------------------------------------------------------------------------
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"

# A book-club recommendation is not worth making the member wait on a slow third party.
# On timeout the caller degrades to an unverified result rather than failing.
HTTP_TIMEOUT_SECONDS = 5.0

# How many volumes to fuzzy-match against. The first hit is usually right, but an exact
# title can rank below an omnibus or a study guide, so consider a short list.
MAX_VOLUMES_CONSIDERED = 5

# Exclude magazines from the results — only books can be recommended.
PRINT_TYPE_BOOKS = "books"

# Ask Google for only the fields the app actually renders. Volume records are large and
# mostly sale/access metadata we never use; this keeps the response (and the cached row)
# small. Session 5's book cards render title, authors, description, page count, cover.
RESPONSE_FIELDS = (
    "items(id,volumeInfo(title,subtitle,authors,description,pageCount,"
    "imageLinks/thumbnail,publishedDate))"
)

# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
# Fraction of the expected title's (and author's) words that must appear in a candidate
# volume for it to count as the same book. 0.8 tolerates subtitles, series suffixes,
# edition noise and punctuation differences ("The Hobbit" vs "The Hobbit, or There and
# Back Again") while still rejecting a merely adjacent book. BOTH title and author must
# clear it — title alone matches study guides and parodies.
MATCH_THRESHOLD = 0.8

# Descriptions from Google run to several thousand characters. Truncate on the way in so
# neither the API response nor the cached row carries more than the UI can use.
MAX_DESCRIPTION_CHARS = 1500
_TRUNCATION_SUFFIX = "…"

# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
# Cache key prefix, so `api_cache` can hold other providers later without collisions.
CACHE_NAMESPACE = "google_books"

# TTL for a cached volume.
#
# WHY 60 DAYS AND NOT 24 HOURS: the original planning note said "cache 24h", which is the
# right default when a cache exists to keep data *fresh*. That is not the job here. A
# published book's id, title, author, page count and cover do not change; this cache
# exists to avoid re-asking Google the same question forever, and the expiry is only
# insurance against a permanently stale row (e.g. Google correcting a record). Do not
# "fix" this back to 24h without a reason that survives that argument.
API_CACHE_TTL_DAYS = _int_env("API_CACHE_TTL_DAYS", 60)

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
# Punctuation is dropped before comparison so "J.R.R. Tolkien" and "J R R Tolkien", or
# "Dune: Book One" and "Dune - Book One", compare as the same words.
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")

# Google still serves cover thumbnails over plain http. Loading one from an https page
# is mixed content and the browser blocks it, so upgrade the scheme on the way in.
_INSECURE_PREFIX = "http://"
_SECURE_PREFIX = "https://"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class GoogleBooksError(Exception):
    """Base class for every Google Books failure."""


class GoogleBooksUnavailable(GoogleBooksError):
    """The Google Books API could not be reached, or answered unusably.

    Deliberately distinct from "no confident match": an outage means we do not KNOW
    whether the book is real, so the caller degrades to an unverified result instead of
    dropping a book that probably exists.
    """


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------
def normalize(value: str) -> str:
    """Lowercase a string, strip punctuation, and collapse runs of whitespace."""
    lowered = (value or "").strip().lower()
    return _WHITESPACE_RE.sub(" ", _PUNCTUATION_RE.sub(" ", lowered)).strip()


def _tokens(value: str) -> set[str]:
    """Return the set of normalized words in a string."""
    normalized = normalize(value)
    return set(normalized.split()) if normalized else set()


def overlap(expected: str, candidate: str) -> float:
    """Return the fraction of `expected`'s words that appear in `candidate` (0.0–1.0).

    Word containment rather than character similarity, because the difference between
    what Claude says and what Google stores is almost always *extra* words — a subtitle,
    a series tag, an edition — not different spelling. "Dune" against "Dune: Book One of
    the Dune Chronicles" scores 1.0; a character-ratio comparison would score it ~0.3
    and throw away a correct match. An empty expectation matches nothing.
    """
    expected_tokens = _tokens(expected)
    if not expected_tokens:
        return 0.0
    return len(expected_tokens & _tokens(candidate)) / len(expected_tokens)


def is_confident_match(title: str, author: str, metadata: dict) -> bool:
    """Return True if a candidate volume is confidently the book we asked for.

    Both the title and the author must clear MATCH_THRESHOLD. Author is compared against
    every credited author joined together, so a book with co-authors still matches when
    Claude named only one of them.
    """
    title_score = overlap(title, metadata["canonical_title"])
    author_score = overlap(author, " ".join(metadata["canonical_authors"]))
    matched = title_score >= MATCH_THRESHOLD and author_score >= MATCH_THRESHOLD
    if not matched:
        logger.debug(
            "Rejected volume %s for %r by %r (title %.2f, author %.2f)",
            metadata["google_books_id"],
            title,
            author,
            title_score,
            author_score,
        )
    return matched


# ---------------------------------------------------------------------------
# Response extraction
# ---------------------------------------------------------------------------
def _extract(item: dict) -> dict | None:
    """Reduce one Google Books volume to the fields this app renders.

    Returns None for a record with no id or no title — either makes the volume useless
    for verification. Missing optional fields come back as None (or an empty author
    list) rather than raising: Google's coverage is uneven, and a book with no cover is
    still a verified book.
    """
    info = item.get("volumeInfo") or {}
    volume_id = (item.get("id") or "").strip()
    title = (info.get("title") or "").strip()
    if not volume_id or not title:
        return None

    subtitle = (info.get("subtitle") or "").strip()
    authors = [a.strip() for a in (info.get("authors") or []) if isinstance(a, str) and a.strip()]

    description = (info.get("description") or "").strip() or None
    if description and len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + _TRUNCATION_SUFFIX

    raw_pages = info.get("pageCount")
    page_count = raw_pages if isinstance(raw_pages, int) and raw_pages > 0 else None

    thumbnail = ((info.get("imageLinks") or {}).get("thumbnail") or "").strip() or None
    if thumbnail and thumbnail.startswith(_INSECURE_PREFIX):
        thumbnail = _SECURE_PREFIX + thumbnail[len(_INSECURE_PREFIX):]

    return {
        "google_books_id": volume_id,
        "canonical_title": f"{title}: {subtitle}" if subtitle else title,
        "canonical_authors": authors,
        "description": description,
        "page_count": page_count,
        "thumbnail_url": thumbnail,
        "published_date": (info.get("publishedDate") or "").strip() or None,
    }


# ---------------------------------------------------------------------------
# HTTP + cache
# ---------------------------------------------------------------------------
def cache_key(title: str, author: str) -> str:
    """Build the `api_cache` key for a (title, author) pair.

    Normalized so trivial differences in casing, spacing or punctuation between two
    Claude responses share one cached row instead of two.
    """
    return f"{CACHE_NAMESPACE}:{normalize(title)}|{normalize(author)}"


async def _search(title: str, author: str) -> list[dict]:
    """Run ONE Google Books query and return its (possibly empty) list of volume items.

    The query is the title and author as free text rather than `intitle:`/`inauthor:`
    operators: the strict operators miss on small wording differences, and precision is
    recovered afterwards by `is_confident_match` instead. The API key is attached only
    if one is configured — the endpoint works keyless at a lower rate limit.

    Raises:
        GoogleBooksUnavailable: network error, timeout, non-2xx status, or a body that
            is not JSON. Never raises for an empty result set — that is a valid answer.
    """
    params = {
        "q": f"{title} {author}".strip(),
        "maxResults": MAX_VOLUMES_CONSIDERED,
        "printType": PRINT_TYPE_BOOKS,
        "fields": RESPONSE_FIELDS,
    }
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if api_key:
        params["key"] = api_key

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(GOOGLE_BOOKS_URL, params=params)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers a non-JSON body (json.JSONDecodeError subclasses it).
        logger.warning("Google Books request failed for %r by %r: %s", title, author, exc)
        raise GoogleBooksUnavailable("Google Books could not be reached.") from exc

    if not isinstance(payload, dict):
        logger.warning("Google Books returned an unexpected body shape for %r", title)
        raise GoogleBooksUnavailable("Google Books returned an unexpected response.")

    return [item for item in (payload.get("items") or []) if isinstance(item, dict)]


async def _write_cache(key: str, metadata: dict) -> None:
    """Persist a confident match, swallowing any failure.

    The cache is an optimization, never a dependency: if the write fails the lookup has
    already succeeded and the member should still get their book. Logged, not raised.
    """
    try:
        await db.set_api_cache(key, metadata, API_CACHE_TTL_DAYS)
    except Exception as exc:  # noqa: BLE001 - deliberate: a cache write must not fail a request
        logger.warning("Could not cache Google Books result for %s: %s", key, exc)


async def lookup(title: str, author: str) -> dict | None:
    """Resolve one (title, author) pair to real Google Books metadata, cache-first.

    Checks `api_cache` before the network and only calls the API on a miss; a confident
    match is written back to the cache. Only positive matches are cached — a title that
    did not verify is usually one Claude invented, and caching that verdict for weeks
    would keep rejecting a book Google might list tomorrow.

    Args:
        title: the title Claude produced.
        author: the author Claude produced.

    Returns:
        A metadata dict (google_books_id, canonical_title, canonical_authors,
        description, page_count, thumbnail_url, published_date) on a confident match,
        or None if nothing Google returned matched confidently.

    Raises:
        GoogleBooksUnavailable: the API could not be reached (distinct from no match).
    """
    key = cache_key(title, author)
    cached = await db.get_api_cache(key)
    if cached is not None:
        logger.debug("Google Books cache hit for %s", key)
        return cached

    for item in await _search(title, author):
        metadata = _extract(item)
        if metadata is not None and is_confident_match(title, author, metadata):
            await _write_cache(key, metadata)
            return metadata

    logger.info("No confident Google Books match for %r by %r", title, author)
    return None
