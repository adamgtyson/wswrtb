"""FastAPI entrypoint: lifespan DB init, routers, static mount, page/health routes,
and the global not-authenticated handler.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.auth import COOKIE_NAME, NeedsLoginException
from app.routes.auth_routes import router as auth_router
from app.routes.profile_routes import router as profile_router

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


@app.exception_handler(NeedsLoginException)
async def handle_needs_login(request: Request, exc: NeedsLoginException):
    """API routes get a 401 JSON; page routes redirect to /login and clear the cookie."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response


app.include_router(auth_router)
app.include_router(profile_router)

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
async def favicon() -> FileResponse | JSONResponse:
    """Serve a favicon if present, else 204-ish empty JSON to keep logs quiet."""
    ico = STATIC_DIR / "favicon.ico"
    if ico.exists():
        return FileResponse(str(ico))
    return JSONResponse(status_code=204, content=None)
