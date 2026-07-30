# WSWRTB

A conversational, group-aware **book-recommendation** web app for book clubs and
families. It currently ships code-gated onboarding, owner/admin group management, and
metered Claude-powered recommendations. Google Books lookup, voting, and reading lists
come in later sessions.

## What it does

- An admin seeds a **book club** (group) and a **seat-limited invite code**.
- New members can register **only** by supplying that invite code — public signup is
  disabled. Redeeming a code links the member to the group and consumes one seat.
- Each member fills a structured **preference profile** (favorite genres/authors, books
  they've loved, dislikes, content limits, reading pace, preferred length) that persists
  across logout/login.
- The **owner** manages the group in-app: view the roster (emails owner-only), remove
  members, and create/deactivate further invite codes.
- Any member can **ask for recommendations**: pick who's reading, describe what you're
  after, and get five books chosen from the selected members' combined profiles. Every
  Claude call is metered against a global daily spend ceiling and per-user rate limits.

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
| `ALLOWED_ORIGINS` | Comma-separated CORS whitelist — no wildcards. Production must set the real domain(s). |
| `RATE_LIMIT_*`    | Optional. Abuse throttles for register/login/invite-create (attempts + window seconds). |

### AI environment variables

`ANTHROPIC_API_KEY` is required for the recommendation feature; without it those
endpoints fail loudly with a 503 rather than degrading silently. Issue the key inside a
dedicated Anthropic Console **workspace with its own hard monthly spend cap** — the
in-app ceilings below are the first line of defence, not the only one.

| Variable                        | Description                                                     |
|---------------------------------|-----------------------------------------------------------------|
| `ANTHROPIC_API_KEY`             | Anthropic API key. Never committed — `.env.example` stays blank. |
| `CLAUDE_MODEL`                  | Model id (default `claude-haiku-4-5`). Changing it changes cost; update the pricing constants in `app/services/claude_service.py` to match. |
| `AI_DAILY_COST_CEILING_USD`     | Global kill switch (default 5). Once ALL users' combined spend for the UTC day reaches this, AI is refused for everyone until the day rolls over. |
| `AI_RATE_PER_HOUR`              | Per-user AI requests per rolling hour (default 15).             |
| `AI_RATE_PER_DAY`               | Per-user AI requests per rolling day (default 50).              |
| `FREE_TIER_MONTHLY_AI_REQUESTS` | Extra cap for `plan = 'free'` users over a rolling 30 days (default 15). Everyone is on `free` today, so in practice this binds first. |
| `AI_MAX_OUTPUT_TOKENS`          | Optional. Max output tokens per Claude call (default 2000).     |
| `AI_MAX_PROMPT_CHARS`           | Optional. Max length of a member's prompt (default 1000).       |

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
- `http://<host>:8000/group` — roster, plus invite-code tools if you're the owner.
- `http://<host>:8000/recommend` — ask for book recommendations.
- `http://<host>:8000/health` — liveness check (`{"status": "ok"}`).
- `http://<host>:8000/docs` — interactive API docs.

## Test

```bash
pytest
```

Covers onboarding (invite codes: missing/invalid/expired/full, seat accounting, duplicate
email, double redemption, profile round-trip), authorization (`require_membership` /
`require_owner`, cross-tenant access), group management (roster, member removal,
invite-code lifecycle), abuse throttling and CORS, and the AI layer (all four cost/rate
controls, usage logging, response parsing, dedup and retry).

**No test ever calls the real Anthropic API** — the client is mocked everywhere, so a
test run can never spend from the workspace budget.

## Known limitations

- No public signup by design — every member needs an invite code.
- No password reset, email verification, or billing yet.
- Recommendations are not yet verified against a book database, so a title Claude
  returns could be wrong or invented. Google Books lookup is the next session.
- The `/recommend` page is a deliberate placeholder — real book cards come later.
- One owner per group; no in-app group creation or renaming.
- Password hashing runs synchronously; fine at book-club scale (see `CLAUDE.md`).
