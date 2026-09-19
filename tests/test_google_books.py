"""Google Books service: matching, enrichment, caching, and failure modes.

No test here touches the real API. Two layers are mocked, for two different jobs:

  * `fake_google_books` (autouse, from conftest) replaces `_search`, so the cache and
    matching logic run for real against synthetic volumes.
  * `real_search` puts the genuine `_search` back and routes httpx through a
    MockTransport, so the HTTP layer — params, status handling, timeouts — is itself
    under test without a socket being opened.
"""
import asyncio
import json

import httpx
import pytest

from app import db
from app.services import google_books

# Captured at import, before the autouse stub replaces it (see `real_search`).
_REAL_SEARCH = google_books._search

# A realistic volume as Google returns it, including the http:// cover URL.
DUNE_VOLUME = {
    "id": "B1hSG45JCX4C",
    "volumeInfo": {
        "title": "Dune",
        "subtitle": "Book One of the Dune Chronicles",
        "authors": ["Frank Herbert"],
        "description": "Set on the desert planet Arrakis.",
        "pageCount": 617,
        "imageLinks": {"thumbnail": "http://books.google.com/books/content?id=B1hSG45JCX4C"},
        "publishedDate": "1965-08-01",
    },
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def real_search(fake_google_books, monkeypatch):
    """Restore the genuine `_search`, so the HTTP layer is the thing being tested.

    Depends on the autouse stub so it is guaranteed to run after it.
    """
    monkeypatch.setattr(google_books, "_search", _REAL_SEARCH)


@pytest.fixture()
def transport(monkeypatch):
    """Route google_books' httpx calls through a MockTransport. Returns an installer.

    Usage: `requests = transport(handler)` — `requests` collects every httpx.Request the
    service made, so a test can assert on the query it built.
    """
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def _install(handler):
        def recording_handler(request):
            requests.append(request)
            return handler(request)

        def factory(**kwargs):
            kwargs["transport"] = httpx.MockTransport(recording_handler)
            return real_client(**kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return requests

    return _install


def json_response(payload, status_code=200):
    """Build a MockTransport handler returning `payload` as JSON."""
    return lambda request: httpx.Response(status_code, json=payload)


# ---------------------------------------------------------------------------
# Normalization + fuzzy matching (pure functions)
# ---------------------------------------------------------------------------
def test_normalize_strips_punctuation_and_case():
    """Punctuation differences must not make two spellings of a name look different."""
    assert google_books.normalize("J.R.R. Tolkien") == google_books.normalize("J R R Tolkien")
    assert google_books.normalize("  Dune:  Book One ") == "dune book one"


def test_overlap_is_word_containment_not_character_similarity():
    """Extra words in the canonical title don't cost overlap — that's the whole point."""
    assert google_books.overlap("Dune", "Dune: Book One of the Dune Chronicles") == 1.0
    assert google_books.overlap("", "Dune") == 0.0
    assert google_books.overlap("The Silent Patient", "The Silent Companions") == pytest.approx(
        2 / 3
    )


def test_subtitle_and_coauthors_still_match():
    """A subtitle on the volume and an unnamed co-author both stay above threshold."""
    metadata = {
        "google_books_id": "x",
        "canonical_title": "Dune: Book One of the Dune Chronicles",
        "canonical_authors": ["Frank Herbert", "Brian Herbert"],
    }
    assert google_books.is_confident_match("Dune", "Frank Herbert", metadata) is True


def test_wrong_book_is_not_a_confident_match():
    """A different title by the same author is rejected — title must clear the bar too."""
    metadata = {
        "google_books_id": "x",
        "canonical_title": "Children of Dune",
        "canonical_authors": ["Frank Herbert"],
    }
    assert google_books.is_confident_match("Dune Messiah Rising", "Frank Herbert", metadata) is False


def test_wrong_author_is_not_a_confident_match():
    """The right title by the wrong author is a study guide or a parody, not the book."""
    metadata = {
        "google_books_id": "x",
        "canonical_title": "Dune",
        "canonical_authors": ["Some Study Guides Inc"],
    }
    assert google_books.is_confident_match("Dune", "Frank Herbert", metadata) is False


# ---------------------------------------------------------------------------
# Volume extraction
# ---------------------------------------------------------------------------
def test_extract_pulls_the_rendered_fields():
    """Every field the UI needs comes across, with the cover upgraded to https."""
    meta = google_books._extract(DUNE_VOLUME)
    assert meta["google_books_id"] == "B1hSG45JCX4C"
    assert meta["canonical_title"] == "Dune: Book One of the Dune Chronicles"
    assert meta["canonical_authors"] == ["Frank Herbert"]
    assert meta["page_count"] == 617
    assert meta["published_date"] == "1965-08-01"
    assert meta["thumbnail_url"].startswith("https://")


def test_extract_tolerates_missing_optional_fields():
    """A book with no cover, description or page count is still a verified book."""
    meta = google_books._extract({"id": "abc", "volumeInfo": {"title": "Sparse"}})
    assert meta["canonical_title"] == "Sparse"
    assert meta["canonical_authors"] == []
    assert meta["description"] is None
    assert meta["page_count"] is None
    assert meta["thumbnail_url"] is None


def test_extract_rejects_a_volume_with_no_id_or_title():
    """Either missing makes the record useless for verification."""
    assert google_books._extract({"volumeInfo": {"title": "No id"}}) is None
    assert google_books._extract({"id": "abc", "volumeInfo": {}}) is None


def test_long_descriptions_are_truncated():
    """Google's descriptions run long; the stored/returned copy is bounded."""
    volume = {"id": "abc", "volumeInfo": {"title": "Wordy", "description": "x" * 5000}}
    meta = google_books._extract(volume)
    assert len(meta["description"]) == google_books.MAX_DESCRIPTION_CHARS + 1  # + the ellipsis


# ---------------------------------------------------------------------------
# HTTP layer (MockTransport — no socket is opened)
# ---------------------------------------------------------------------------
def test_confident_match_returns_metadata(client, real_search, transport):
    """End to end over the real HTTP path: a matching volume comes back enriched."""
    transport(json_response({"items": [DUNE_VOLUME]}))
    meta = _run(google_books.lookup("Dune", "Frank Herbert"))
    assert meta["google_books_id"] == "B1hSG45JCX4C"
    assert meta["page_count"] == 617


def test_low_overlap_result_is_treated_as_no_match(client, real_search, transport):
    """Google answered, but with a different book — that's a None, not a match."""
    transport(json_response({"items": [DUNE_VOLUME]}))
    assert _run(google_books.lookup("The Silent Patient", "Alex Michaelides")) is None


def test_empty_result_set_is_no_match(client, real_search, transport):
    """An invented title Google has never heard of returns nothing, and that's fine."""
    transport(json_response({}))
    assert _run(google_books.lookup("The Cartographer of Lost Hours", "Nobody")) is None


def test_the_first_matching_volume_wins(client, real_search, transport):
    """Junk ranked above the real book doesn't stop the lookup finding it."""
    junk = {"id": "junk", "volumeInfo": {"title": "Dune Study Guide", "authors": ["SparkNotes"]}}
    transport(json_response({"items": [junk, DUNE_VOLUME]}))
    meta = _run(google_books.lookup("Dune", "Frank Herbert"))
    assert meta["google_books_id"] == "B1hSG45JCX4C"


def test_query_asks_only_for_the_fields_we_render(client, real_search, transport):
    """Volume records are huge; the request narrows them to what the UI uses."""
    requests = transport(json_response({"items": [DUNE_VOLUME]}))
    _run(google_books.lookup("Dune", "Frank Herbert"))
    params = requests[0].url.params
    assert params["q"] == "Dune Frank Herbert"
    assert params["fields"] == google_books.RESPONSE_FIELDS
    assert int(params["maxResults"]) == google_books.MAX_VOLUMES_CONSIDERED


def test_api_key_is_optional(client, real_search, transport, monkeypatch):
    """Keyless is a supported mode — the key param is attached only when one is set."""
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)
    requests = transport(json_response({"items": [DUNE_VOLUME]}))
    _run(google_books.lookup("Dune", "Frank Herbert"))
    assert "key" not in requests[0].url.params


def test_api_key_is_sent_when_configured(client, real_search, transport, monkeypatch):
    """When a key is configured it rides on the request (higher rate limits)."""
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "test-key-value")
    requests = transport(json_response({"items": [DUNE_VOLUME]}))
    _run(google_books.lookup("Dune", "Frank Herbert"))
    assert requests[0].url.params["key"] == "test-key-value"


def test_timeout_raises_unavailable(client, real_search, transport):
    """A slow third party must be distinguishable from 'this book isn't real'."""

    def handler(request):
        raise httpx.TimeoutException("too slow", request=request)

    transport(handler)
    with pytest.raises(google_books.GoogleBooksUnavailable):
        _run(google_books.lookup("Dune", "Frank Herbert"))


def test_http_error_raises_unavailable(client, real_search, transport):
    """A 503 from Google is an outage, not an answer about the book."""
    transport(json_response({"error": "unavailable"}, status_code=503))
    with pytest.raises(google_books.GoogleBooksUnavailable):
        _run(google_books.lookup("Dune", "Frank Herbert"))


def test_unexpected_body_shape_raises_unavailable(client, real_search, transport):
    """Valid JSON that isn't a volume envelope is an outage, not a silent empty result."""
    transport(json_response([1, 2, 3]))
    with pytest.raises(google_books.GoogleBooksUnavailable):
        _run(google_books.lookup("Dune", "Frank Herbert"))


def test_non_json_body_raises_unavailable(client, real_search, transport):
    """An HTML error page (a proxy, a captive portal) is handled, not a stack trace."""
    transport(lambda request: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(google_books.GoogleBooksUnavailable):
        _run(google_books.lookup("Dune", "Frank Herbert"))


# ---------------------------------------------------------------------------
# api_cache
# ---------------------------------------------------------------------------
def test_cache_hit_skips_the_api_entirely(client, fake_google_books, fetchone):
    """A cached row answers the lookup with no outbound request at all."""
    key = google_books.cache_key("Dune", "Frank Herbert")
    payload = {"google_books_id": "cached-id", "canonical_title": "Dune"}
    _run(db.set_api_cache(key, payload, google_books.API_CACHE_TTL_DAYS))

    meta = _run(google_books.lookup("Dune", "Frank Herbert"))
    assert meta == payload
    assert fake_google_books.call_count == 0


def test_cache_miss_calls_the_api_then_writes_the_cache(client, fake_google_books, fetchone):
    """First lookup hits the API and stores the result; the second is served from SQLite."""
    first = _run(google_books.lookup("Dune", "Frank Herbert"))
    assert fake_google_books.call_count == 1
    assert fetchone("SELECT COUNT(*) FROM api_cache")[0] == 1

    second = _run(google_books.lookup("Dune", "Frank Herbert"))
    assert second == first
    assert fake_google_books.call_count == 1  # still one — the cache answered


def test_cache_key_normalizes_trivial_differences(client, fake_google_books):
    """Casing and punctuation shouldn't split one book across two cached rows."""
    _run(google_books.lookup("Dune", "Frank Herbert"))
    _run(google_books.lookup("  dune ", "frank  herbert"))
    assert fake_google_books.call_count == 1


def test_expired_cache_entry_is_treated_as_a_miss(client, fake_google_books, fetchone):
    """A row past its TTL is re-fetched and overwritten, not served."""
    key = google_books.cache_key("Dune", "Frank Herbert")
    _run(db.set_api_cache(key, {"google_books_id": "stale"}, ttl_days=-1))

    meta = _run(google_books.lookup("Dune", "Frank Herbert"))
    assert fake_google_books.call_count == 1
    assert meta["google_books_id"] != "stale"
    # Refreshed in place rather than duplicated.
    assert fetchone("SELECT COUNT(*) FROM api_cache")[0] == 1


def test_unverified_lookups_are_not_cached(client, fake_google_books, fetchone):
    """Only confident matches are cached — see the note in google_books.lookup."""
    fake_google_books.never_matches("The Cartographer of Lost Hours")
    assert _run(google_books.lookup("The Cartographer of Lost Hours", "Nobody")) is None
    assert fetchone("SELECT COUNT(*) FROM api_cache")[0] == 0


def test_corrupt_cache_row_is_treated_as_a_miss(client, fake_google_books):
    """An unparseable row must not break a lookup."""
    key = google_books.cache_key("Dune", "Frank Herbert")

    async def corrupt():
        async with db.connect() as conn:
            await conn.execute(
                "INSERT INTO api_cache (cache_key, response_json, cached_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (key, "not json", db.utcnow_str(), "2099-01-01 00:00:00"),
            )
            await conn.commit()

    _run(corrupt())
    assert _run(google_books.lookup("Dune", "Frank Herbert")) is not None
    assert fake_google_books.call_count == 1


def test_cache_write_failure_does_not_fail_the_lookup(client, fake_google_books, monkeypatch):
    """The cache is an optimization, never a dependency."""

    async def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(db, "set_api_cache", boom)
    meta = _run(google_books.lookup("Dune", "Frank Herbert"))
    assert meta["google_books_id"]


def test_cached_payload_round_trips_as_json(client, fetchone):
    """What goes into api_cache is the metadata dict, stored as JSON."""
    _run(db.set_api_cache("probe:key", {"a": 1, "b": ["x"]}, 1))
    stored = fetchone("SELECT response_json FROM api_cache WHERE cache_key = ?", ("probe:key",))
    assert json.loads(stored[0]) == {"a": 1, "b": ["x"]}
    assert _run(db.get_api_cache("probe:key")) == {"a": 1, "b": ["x"]}
