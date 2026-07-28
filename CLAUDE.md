# WSWRTB — Living Project Doc

**WSWRTB** — a conversational, group-aware book-recommendation web app for book clubs
and families (sibling of the WSWWTB movie app). This file is the durable source of truth for
architecture, conventions, and build state. Update the **Current Build State** and
**Pending / on the horizon** sections at the end of every session (overwrite, do not append).

---

## Architecture summary

A FastAPI backend serving a vanilla HTML/CSS/JS frontend, backed by SQLite. The product
is organized around **groups** (book clubs or families) whose **members** each keep a
preference **profile**. Recommendations, voting, and AI cost controls are later sessions;
Session 1 builds only the onboarding vertical slice.

```
app/
├── main.py            FastAPI entrypoint: routers, static mount, page routes, health, auth error handler
├── db.py              SQLite schema init (CREATE TABLE IF NOT EXISTS) + async connection helpers
├── auth.py            bcrypt hashing (passlib), JWT issue/verify, get_current_user dependency
├── deps.py            require_membership(...) authorization dependency factory
├── models.py          Pydantic request/response schemas + server-side validation
├── routes/
│   ├── auth_routes.py     /api/register (code-gated), /api/login, /api/logout, + page routes
│   └── profile_routes.py  /api/profile GET/PUT, + protected group demo route
└── services/
    └── invites.py     Atomic invite-code redemption + account creation

static/                signup.html, login.html, profile.html + css/ + js/
scripts/seed_group.py  CLI to create the first owner, group, and seat-limited invite code
tests/                 pytest suite (registration gating, seat cap, profile round-trip, authz)
```

### Data flow (onboarding)

1. Admin runs `scripts/seed_group.py` → creates an owner user, a group, an owner
   membership, and a seat-limited `invite_code`.
2. A visitor opens `/signup`, supplies email + password + display name + the club's
   invite code.
3. `POST /api/register` runs `services/invites.redeem_and_register` in **one atomic
   transaction**: validate/decrement the seat, create the user, link a `member`
   membership to the code's group, record the redemption. Fills the seat or rejects
   with a generic error.
4. On success a JWT session cookie is set; the browser lands on `/profile`.
5. The visitor fills the structured preference form → `PUT /api/profile` persists JSON
   columns on their `users` row. Data round-trips across logout/login.

---

## Conventions (rules, not suggestions)

- **Stack is non-negotiable:** FastAPI · SQLite · vanilla HTML/CSS/JS (no frontend
  frameworks, ever) · JWT session cookies · bcrypt (via passlib). No CSS framework.
- **All SQL is parameterized.** Never build a query with f-strings, `.format`, `%`, or
  string concatenation — always pass values as `?` parameters.
- **`PRAGMA foreign_keys = ON`** on every connection (helper does this).
- **Server-side validation on every input** via Pydantic. Never trust the client.
- **Secrets only from `.env`** via python-dotenv. `.env` is git-ignored; `.env.example`
  is committed with blank values. Nothing hardcoded.
- **Cookie flags:** httpOnly, SameSite=Lax, and `Secure` **gated by `ENVIRONMENT`**
  (on in production, off in development so plain-http LAN testing works). Never ship
  Secure-off to production.
- **Generic auth/registration errors.** Never reveal which field or condition failed
  ("That invite code is invalid or full." / "Invalid email or password.").
- **Invite redemption is atomic** — the seat check-and-decrement and the account
  creation happen in a single transaction so concurrent redemptions cannot overfill.
- **Dev on "the-rig" (this dev server). Never edit on the Droplet.** Production is a
  DigitalOcean Droplet; deploys are pulls, not in-place edits. Keep everything portable
  Ubuntu-standard. Local dev only this session — do not deploy.
- Every function gets a docstring. No magic strings/numbers in logic — use module
  constants.
- The JSON preference columns are the **single source of truth** for a member's
  profile. A future conversational (Claude) discovery layer must augment these SAME
  columns — do not fork the data model.

### Runtime / tooling notes

- Target runtime is **Python 3.12** (matches the production Droplet). This dev box's
  default `python3` is 3.14; build the venv explicitly with `python3.12`.
- Hashing/verify are synchronous (passlib). bcrypt's ~200ms cost per call briefly
  occupies the event loop; acceptable at book-club scale. Revisit (threadpool offload)
  only if signup/login throughput ever matters.

---

## Database schema

Full schema lives in `app/db.py`, all `CREATE TABLE IF NOT EXISTS` so later sessions
need no migration. Session 1 **actively uses** `users`, `groups`, `memberships`,
`invite_codes`, `invite_redemptions`. The rest (`feedback`, `reading_list`,
`voting_rounds`, `ballots`, `ai_usage`, `recent_searches`, `api_cache`, `flags`) are
**forward-declared** — created now, unused this session.

Invite model: `invite_codes` (seat-limited, multi-redemption) + `invite_redemptions`
(one row per successful redemption, audit trail + UNIQUE guard). This supersedes any
single-use `invites` table. One invite code maps to one group.

---

## Current Build State

_Session 1 complete — Scaffold + Code-Gated Onboarding vertical slice._

Built and passing (20 tests, 89% coverage):

- **Auth** (`app/auth.py`): bcrypt via passlib, HS256 JWT in an httpOnly + SameSite=Lax
  cookie (`session`), `Secure` gated on `ENVIRONMENT`, 7-day expiry, `JWT_SECRET` from
  `.env` (min 32 chars). `get_current_user` dependency; `NeedsLoginException` handled
  globally (401 JSON on `/api/`, redirect to `/login` on pages).
- **DB** (`app/db.py`): full schema via `CREATE TABLE IF NOT EXISTS` (active: users,
  groups, memberships, invite_codes, invite_redemptions; the rest forward-declared).
  `connect()` is an `@asynccontextmanager` enforcing `PRAGMA foreign_keys = ON`; it
  takes `isolation_level` at creation time so manual-transaction callers set it on
  aiosqlite's worker thread. All SQL parameterized.
- **Code-gated registration** (`app/services/invites.py`): public signup disabled;
  requires email + password + display_name + invite_code. One atomic transaction
  (`BEGIN IMMEDIATE`, conditional `UPDATE ... RETURNING` consumes the seat first, then
  user/membership/redemption inserts; any failure rolls back and releases the seat).
  `active` flips to 0 at the cap. Generic error on every failure.
- **Authorization** (`app/deps.py`): `require_membership(...)` factory; wired onto
  `GET /api/groups/{group_id}` (403 non-member, 401 unauth, 404 unknown group).
- **Profile** (`app/routes/profile_routes.py`, `app/models.py`): structured builder
  (genres/authors/examples/dislikes lists, content_preferences, reading_pace,
  preferred_length) persisted as JSON columns on `users`; round-trips across
  logout/login. Pydantic validation on every input.
- **Seed script** (`scripts/seed_group.py`): creates owner + group + owner membership +
  seat-limited invite code; `--code` normalized/validated (4–32 chars, letters/digits/
  hyphens), random if omitted.
- **Frontend** (`static/`): vanilla signup/login/profile pages, mobile-first, 680px
  centered, dark/light toggle in localStorage.
- **DoD verified**: seeded an 8-seat code, 8 members registered (201), the 9th refused
  (400); lowercase codes normalize; profile persists across logout/login; no-code
  registration impossible.

---

## Pending / on the horizon

- **Next (Session 2):** owner/admin member-management UI (create/rotate codes, view
  seats, remove members) — not just the seed script.
- Session 3: metered Claude service (cost controls, `ai_usage` accounting) — the
  prerequisite for the conversational profile-discovery layer.
- Later: recommendation engine, Google Books integration, voting rounds/ballots,
  reading lists, cross-group matching, flags review.
- Not yet: password reset, email verification, Stripe/billing, managed auth (Clerk).

**Housekeeping noticed this session (not blocking):**
- `pytest-cov` is used for the coverage report but is not in `requirements.txt` (which
  pins production deps only). Add a `requirements-dev.txt` if/when coverage becomes part
  of CI.
- `session1_summary.txt` is a stray scratch file in the repo root (left untracked, not
  committed). Delete it when convenient.

---

## Blocked / Needs Adam

_None. Add entries here if a dependency won't install, a requirement conflicts with
reality, or a decision is ambiguous — stop and record it rather than guessing._
