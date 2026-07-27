"""Authentication primitives: password hashing (bcrypt via passlib), JWT session
tokens, and the get_current_user FastAPI dependency.

Hashing/verify are synchronous — passlib is sync, and bcrypt's cost briefly occupies
the event loop. Acceptable at book-club scale (see CLAUDE.md runtime notes).
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Request
from passlib.context import CryptContext

from app import db

logger = logging.getLogger(__name__)

COOKIE_NAME = "session"
_ALGORITHM = "HS256"
_MIN_SECRET_LEN = 32

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _jwt_secret() -> str:
    """Return the JWT signing secret, or raise if missing/too short."""
    secret = os.environ.get("JWT_SECRET", "")
    if not secret or len(secret) < _MIN_SECRET_LEN:
        raise RuntimeError(
            f"JWT_SECRET must be set to a minimum {_MIN_SECRET_LEN}-character string in .env. "
            'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
    return secret


def _expiry_days() -> int:
    """Session lifetime in days, from JWT_EXPIRY_DAYS (default 7)."""
    return int(os.environ.get("JWT_EXPIRY_DAYS", "7"))


class NeedsLoginException(Exception):
    """Raised when a request has no valid session. Handled globally in main.py."""


def hash_password(password: str) -> str:
    """Return a bcrypt hash of the given plaintext password."""
    return _pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Return True if the plaintext password matches the stored bcrypt hash."""
    try:
        return _pwd_context.verify(password, hashed)
    except ValueError:
        # Malformed/unknown hash — treat as a failed login, never crash.
        return False


def create_session_token(user_id: int, email: str) -> str:
    """Create a signed JWT carrying the user id and email, expiring in JWT_EXPIRY_DAYS."""
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(days=_expiry_days()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_ALGORITHM)


def cookie_max_age() -> int:
    """Session cookie max-age in seconds, matching the JWT expiry."""
    return 60 * 60 * 24 * _expiry_days()


def secure_cookies() -> bool:
    """Secure flag is on only in production (localhost/LAN is plain http in dev)."""
    return os.environ.get("ENVIRONMENT", "development") == "production"


async def get_current_user(request: Request) -> dict:
    """FastAPI dependency: resolve the authenticated user from the session cookie.

    Returns {id, email, display_name}. Raises NeedsLoginException if the cookie is
    absent, invalid, expired, or points at a user that no longer exists.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise NeedsLoginException()
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        raise NeedsLoginException()

    user = await db.get_user_by_id(payload.get("user_id"))
    if user is None:
        raise NeedsLoginException()
    return user
