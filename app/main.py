"""FastAPI entrypoint: lifespan DB init, routers, static mount, page/health routes,
and the global not-authenticated handler.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.auth import COOKIE_NAME, NeedsLoginException
from app.routes.auth_routes import router as auth_router
from app.routes.group_routes import router as group_router
from app.routes.profile_routes import router as profile_router
from app.services.rate_limit import RateLimitError

load_dotenv()

STATIC_DIR = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Initialize the database schema on startup."""
    await db.init_db()
    yield


app = FastAPI(
    title="WSWRTB",
    description="Group-aware book recommendations for book clubs and families. Session 1: onboarding.",
    lifespan=lifespan,
)


def _allowed_origins() -> list[str]:
    """Parse ALLOWED_ORIGINS (comma-separated) from the environment into a list.

    Empty/unset means no cross-origin requests are permitted — never a wildcard, since
    the app authenticates with cookies (allow_credentials=True forbids "*" anyway).
    """
    raw = os.environ.get("ALLOWED_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# Explicit CORS whitelist. Methods/headers are restricted to what the app actually uses
# rather than wildcarded. Production must set the real deployed origin(s) in .env.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.exception_handler(NeedsLoginException)
async def handle_needs_login(request: Request, exc: NeedsLoginException):
    """API routes get a 401 JSON; page routes redirect to /login and clear the cookie."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.exception_handler(RateLimitError)
async def handle_rate_limit(request: Request, exc: RateLimitError):
    """Map a tripped rate limit to a generic HTTP 429 (no timing/quota detail leaked)."""
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many requests. Please slow down and try again later."},
    )


app.include_router(auth_router)
app.include_router(profile_router)
app.include_router(group_router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/health", summary="Health check", tags=["system"])
async def health() -> dict:
    """Return 200 OK if the service is running."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send visitors to their profile; the profile page bounces to /login if not authed."""
    return RedirectResponse(url="/profile", status_code=302)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """Serve a favicon if present, else a 204 to keep request logs quiet."""
    ico = STATIC_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico))
    return Response(status_code=204)
