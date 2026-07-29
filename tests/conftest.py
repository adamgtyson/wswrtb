"""Test fixtures: isolated temp DB, a fresh app client per test, a seed helper, and a
raw-query helper. Env vars are set before any app import so db.py picks up the temp DB.
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
