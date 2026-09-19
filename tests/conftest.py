"""Test fixtures: isolated temp DB, a fresh app client per test, a seed helper, a
raw-query helper, and a mocked Claude client. Env vars are set before any app import so
db.py picks up the temp DB.

NOTHING in this suite may call the real Anthropic API. Every AI test installs the
`fake_claude` fixture, which replaces the SDK client entirely — a test run must never
issue a real request or spend a cent.

The same rule covers Google Books, with one difference: its stub is AUTOUSE, so no test
can reach that API even by forgetting to ask. `fake_google_books` replaces the service's
HTTP layer only (`_search`), leaving the cache, matching and enrichment logic under real
test. Tests that need the HTTP layer itself exercise it through a mocked transport.
"""
import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("JWT_SECRET", "test_jwt_secret_minimum_32_characters!!")
os.environ.setdefault("JWT_EXPIRY_DAYS", "7")
os.environ.setdefault("ENVIRONMENT", "development")
# Fixed origin so CORS tests have a known allowed value.
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:8000")
# Generous rate limits so functional tests aren't throttled; the rate-limit tests
# override these per-test via monkeypatch to exercise the trip.
os.environ.setdefault("RATE_LIMIT_REGISTER", "100000")
os.environ.setdefault("RATE_LIMIT_LOGIN", "100000")
os.environ.setdefault("RATE_LIMIT_INVITE_CREATE", "100000")

# Point the app at a throwaway DB file before importing anything that reads DB_PATH.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["WSWRTB_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import claude_service, google_books  # noqa: E402

OWNER_PASSWORD = "ownerpass1"


def _run(coro):
    """Run an async coroutine to completion in a throwaway event loop."""
    return asyncio.run(coro)


@pytest.fixture()
def client():
    """A TestClient backed by a pristine database (schema re-created per test)."""
    if os.path.exists(_tmp.name):
        os.remove(_tmp.name)
    with TestClient(app) as c:  # entering runs the lifespan -> init_db()
        yield c


@pytest.fixture()
def seed(client):
    """Create an owner + group + invite code. Returns the created ids/code dict."""

    def _seed(code="TESTCLUB", seats=8, expires_at=None, email="owner@example.com",
              group_name="Test Club", group_type="book_club"):
        return _run(
            db.create_owner_group_and_invite(
                email=email,
                password_hash=auth.hash_password(OWNER_PASSWORD),
                display_name="Owner",
                group_name=group_name,
                group_type=group_type,
                code=code,
                max_redemptions=seats,
                expires_at=expires_at,
            )
        )

    return _seed


@pytest.fixture()
def fetchone():
    """Run a parameterized SELECT and return the first row (or None)."""

    def _fetchone(sql, params=()):
        async def go():
            async with db.connect() as conn:
                async with conn.execute(sql, params) as cur:
                    return await cur.fetchone()

        return _run(go())

    return _fetchone


# ---------------------------------------------------------------------------
# Mocked Claude client
# ---------------------------------------------------------------------------
class FakeBlock:
    """Stand-in for an Anthropic text content block."""

    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeUsage:
    """Stand-in for the response usage field the service reads token counts from."""

    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class FakeResponse:
    """Stand-in for a Messages API response."""

    def __init__(self, text, input_tokens, output_tokens):
        self.content = [FakeBlock(text)]
        self.usage = FakeUsage(input_tokens, output_tokens)


def claude_response(text, input_tokens=1000, output_tokens=300):
    """Build a fake Claude response carrying `text` and explicit token counts."""
    return FakeResponse(text, input_tokens, output_tokens)


class FakeClaudeClient:
    """Records calls and returns queued responses; never touches the network.

    Exposes `.messages.create(**kwargs)` like the real async SDK client. Each call pops
    the next queued response; an exception instance is raised instead of returned. Running
    out of responses is an assertion failure, so an unexpected extra API call (e.g. a
    retry loop that doesn't terminate) fails the test loudly rather than hanging.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    @property
    def messages(self):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        assert self._responses, f"Unexpected extra Claude call #{len(self.calls)}"
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    @property
    def call_count(self):
        return len(self.calls)


@pytest.fixture()
def fake_claude(monkeypatch):
    """Install a fake Claude client. Usage: `fake = fake_claude(resp1, resp2, ...)`."""

    def _install(*responses):
        fake = FakeClaudeClient(responses)
        monkeypatch.setattr(claude_service, "get_client", lambda: fake)
        return fake

    return _install


# ---------------------------------------------------------------------------
# Mocked Google Books client
# ---------------------------------------------------------------------------
# Page count and cover used by the synthetic volumes below. The thumbnail is deliberately
# http:// — Google really does serve them that way, and the service upgrades the scheme.
FAKE_PAGE_COUNT = 321
FAKE_THUMBNAIL = "http://books.google.com/books/content?id=fake&img=1"
FAKE_PUBLISHED_DATE = "2001-05-01"


def google_volume(title, author, volume_id=None, subtitle=None):
    """Build one Google Books API item, shaped like the real `items[]` entries."""
    info = {
        "title": title,
        "authors": [author],
        "description": "A book that really exists.",
        "pageCount": FAKE_PAGE_COUNT,
        "imageLinks": {"thumbnail": FAKE_THUMBNAIL},
        "publishedDate": FAKE_PUBLISHED_DATE,
    }
    if subtitle:
        info["subtitle"] = subtitle
    return {"id": volume_id or ("gb-" + title.lower().replace(" ", "-")), "volumeInfo": info}


class FakeGoogleBooks:
    """Stand-in for google_books._search. Records every query; never touches the network.

    Default behaviour is that every book exists — so the tests written before Session 4
    keep passing unchanged and get verified results. Individual tests opt into the other
    two outcomes with `never_matches()` (Google answers, nothing matches) and `fail()`
    (the API is unreachable).
    """

    def __init__(self):
        self.calls = []
        self._unmatchable = set()
        self._failure = None

    def never_matches(self, *titles):
        """Make these titles return no usable volume — the 'Claude invented it' case."""
        self._unmatchable.update(google_books.normalize(t) for t in titles)

    def fail(self, exc=None):
        """Make every lookup raise — the 'Google Books is down' case."""
        self._failure = exc or google_books.GoogleBooksUnavailable("simulated outage")

    async def search(self, title, author):
        self.calls.append((title, author))
        if self._failure is not None:
            raise self._failure
        if google_books.normalize(title) in self._unmatchable:
            return []
        return [google_volume(title, author)]

    @property
    def call_count(self):
        return len(self.calls)


@pytest.fixture(autouse=True)
def fake_google_books(monkeypatch):
    """Cut the Google Books network for EVERY test, matched by default.

    Autouse on purpose: an un-stubbed lookup would make a real outbound request, and a
    test suite that quietly depends on a third party is a test suite that fails on a
    train. Patching `_search` leaves caching, fuzzy matching and enrichment real.
    """
    fake = FakeGoogleBooks()
    monkeypatch.setattr(google_books, "_search", fake.search)
    return fake


def register(client, code, email="member@example.com", password="password1",
             display_name="Member"):
    """Helper: POST /api/register with the given fields."""
    return client.post(
        "/api/register",
        json={
            "email": email,
            "password": password,
            "display_name": display_name,
            "invite_code": code,
        },
    )
