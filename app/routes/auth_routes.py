"""Authentication and registration routes, plus the public HTML page routes.

Registration is code-gated: no valid invite code, no account. All auth/registration
failures return a generic message so no single failing condition is leaked.
"""
from pathlib import Path

from fastapi import APIRouter, status
from fastapi.responses import FileResponse, JSONResponse

from app import auth, db
from app.models import LoginRequest, RegisterRequest
from app.services.invites import InviteError, redeem_and_register

router = APIRouter()

_STATIC = Path(__file__).parent.parent.parent / "static"


def _session_response(content: dict, user_id: int, email: str, status_code: int = 200) -> JSONResponse:
    """Build a JSON response that also sets the signed session cookie."""
    token = auth.create_session_token(user_id, email)
    response = JSONResponse(content=content, status_code=status_code)
    response.set_cookie(
        key=auth.COOKIE_NAME,
        value=token,
        httponly=True,
        secure=auth.secure_cookies(),
        samesite="lax",
        max_age=auth.cookie_max_age(),
    )
    return response


# ----- Page routes (public HTML) -----
@router.get("/signup", include_in_schema=False)
async def signup_page() -> FileResponse:
    return FileResponse(str(_STATIC / "signup.html"))


@router.get("/login", include_in_schema=False)
async def login_page() -> FileResponse:
    return FileResponse(str(_STATIC / "login.html"))


# ----- API routes -----
@router.post(
    "/api/register",
    summary="Register a new member via invite code",
    description=(
        "Code-gated registration. Requires email, password, display_name, and a valid "
        "invite_code. On success creates the user, links them to the code's group, "
        "consumes a seat, and sets the session cookie."
    ),
    tags=["auth"],
)
async def register(body: RegisterRequest) -> JSONResponse:
    """Create an account by redeeming an invite code, then log the user in."""
    try:
        result = await redeem_and_register(
            email=body.email,
            password=body.password,
            display_name=body.display_name,
            invite_code=body.invite_code,
        )
    except InviteError:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "That invite code is invalid or full."},
        )
    return _session_response(
        {"success": True, "group_id": result["group_id"]},
        result["user_id"],
        body.email,
        status_code=status.HTTP_201_CREATED,
    )


@router.post(
    "/api/login",
    summary="Log in with email and password",
    tags=["auth"],
)
async def login(body: LoginRequest) -> JSONResponse:
    """Verify credentials and set the session cookie. Generic error on failure."""
    user = await db.get_user_by_email(body.email)
    if user is None or not auth.verify_password(body.password, user["password_hash"]):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid email or password."},
        )
    return _session_response({"success": True}, user["id"], user["email"])


@router.post(
    "/api/logout",
    summary="Log out (clear the session cookie)",
    tags=["auth"],
)
async def logout() -> JSONResponse:
    """Clear the session cookie."""
    response = JSONResponse(content={"success": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response
