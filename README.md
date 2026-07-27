# WSWRTB

A conversational, group-aware **book-recommendation** web app for book clubs and
families. Session 1 ships the onboarding vertical slice: code-gated registration,
authentication, and a structured preference profile. Recommendations, voting, and AI
cost controls come in later sessions.

## What it does (Session 1)

- An admin seeds a **book club** (group) and a **seat-limited invite code**.
- New members can register **only** by supplying that invite code — public signup is
  disabled. Redeeming a code links the member to the group and consumes one seat.
- Each member fills a structured **preference profile** (favorite genres/authors, books
  they've loved, dislikes, content limits, reading pace, preferred length) that persists
  across logout/login.

## Stack — and why

| Layer     | Choice                        | Why |
|-----------|-------------------------------|-----|
| Backend   | FastAPI                       | Async, Pydantic validation, auto OpenAPI docs at `/docs`. |
| Database  | SQLite (`aiosqlite`)          | Zero-ops, file-based, ideal for a single-box app; portable to the Droplet. |
| Frontend  | Vanilla HTML/CSS/JS           | No build step, no framework churn; hand-written and small. |
| Auth      | JWT in an httpOnly cookie     | Stateless sessions, no server-side session store. |
| Passwords | bcrypt via passlib            | Proven, salted, tunable cost. |

## Requirements

- **Python 3.12** (matches the production Droplet). This dev box's default `python3` is
  3.14 — build the venv explicitly with `python3.12`.

## Setup

```bash
# 1. Clone, then create and activate a virtualenv (use python3.12 explicitly)
python3.12 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your .env from the template and set a JWT secret
cp .env.example .env
python3 -c "import secrets; print('JWT_SECRET=' + secrets.token_hex(32))"
#   -> paste the printed line into .env (replacing the blank JWT_SECRET=)
```

### Required environment variables

| Variable          | Description                                                        |
|-------------------|--------------------------------------------------------------------|
| `JWT_SECRET`      | Signing key for session cookies. **Min 32 chars.** Required.       |
| `JWT_EXPIRY_DAYS` | Session lifetime in days (default 7).                              |
| `ENVIRONMENT`     | `development` or `production`. Gates the cookie `Secure` flag.     |
| `WSWRTB_DB_PATH`  | Optional. Override the SQLite file path (defaults to `./wswrtb.db`). |

## Seed the first club and invite code

The seed script creates an owner user, a group, an owner membership, and one
seat-limited invite code. The `--code` is a human-friendly string **you** supply
(letters/digits/hyphens, 4–32 chars); omit it to auto-generate a random one.

```bash
python scripts/seed_group.py \
  --email owner@example.com \
  --password 'a-strong-password' \
  --display-name 'Club Owner' \
  --group-name 'The Night Owls' \
  --code NIGHTOWLS-2026 \
  --seats 8
```

This creates an **8-seat** code `NIGHTOWLS-2026`. The 9th registration on it is refused
as full.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then open:

- `http://<host>:8000/signup` — register with your email, password, name, and the club code.
- `http://<host>:8000/login` — log in.
- `http://<host>:8000/profile` — edit your preference profile.
- `http://<host>:8000/health` — liveness check (`{"status": "ok"}`).
- `http://<host>:8000/docs` — interactive API docs.

## Test

```bash
pytest
```

Covers: registration rejects missing/invalid/expired/full codes; a valid code creates
the membership and increments the seat count; the code deactivates at the cap; duplicate
email is rejected; a user can't redeem the same code twice; profile data round-trips;
and `require_membership` returns 403 for non-members, 200 for members.

## Known limitations (Session 1)

- No public signup by design — every member needs an invite code.
- No password reset, email verification, or billing yet.
- No owner/admin UI beyond the CLI seed script (Session 2).
- No recommendations, book API, AI, or voting yet.
- Password hashing runs synchronously; fine at book-club scale (see `CLAUDE.md`).
