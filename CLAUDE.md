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
├── deps.py            require_membership(...) / require_owner(...) authorization factories
├── models.py          Pydantic request/response schemas + server-side validation
├── routes/
│   ├── auth_routes.py     /api/register (code-gated), /api/login, /api/logout, + page routes
│   ├── profile_routes.py  /api/profile GET/PUT, + /api/groups/{id} (role-aware) demo route
│   └── group_routes.py    owner tooling: member roster/removal, invite-code lifecycle, /group page
└── services/
    └── invites.py     Atomic invite-code redemption + account creation + code gen/normalization

static/                signup.html, login.html, profile.html, group.html + css/ + js/
scripts/seed_group.py  CLI to create the first owner, group, and seat-limited invite code
tests/                 pytest suite (onboarding + group management: gating, seats, profile, authz)
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

_Sessions 1–2 complete. 32 tests passing, 89% coverage._

**Session 1 — Scaffold + Code-Gated Onboarding:**

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
- **Profile** (`app/routes/profile_routes.py`, `app/models.py`): structured builder
  persisted as JSON columns on `users`; round-trips across logout/login.
- **Seed script** (`scripts/seed_group.py`): creates the FIRST owner + group + owner
  membership + seat-limited invite code. Unchanged this session.

**Session 2 — Owner/Admin Member Management (in-app tooling after the first code):**

- **Authorization** (`app/deps.py`): `require_membership(...)` (any member) and the new
  `require_owner(...)` (role = `'owner'`). Both resolve role server-side from the JWT +
  `memberships` table; owner-only endpoints 403 for members and cross-tenant owners.
- **Member routes** (`app/routes/group_routes.py`):
  - `GET /api/groups/{id}/members` — any member sees the roster (display_name, role,
    joined_at); **emails only for owners**.
  - `DELETE /api/groups/{id}/members/{user_id}` — owner-only; deletes just the
    membership row (account/profile untouched); **refuses to remove the owner (400)**;
    non-member removal is 404.
- **Invite-code routes** (owner-only, same file): `GET` list, `POST` create (optional
  custom code via the shared Session 1 normalizer; seats 1–200; random if omitted;
  409 on duplicate; multiple active codes per group allowed), `PATCH .../deactivate`
  (manual `active=0`, idempotent no-op, 404 for a code not in the group).
- `GET /api/groups/{id}` extended to include the caller's `role` (authorization
  unchanged); `GET /api/me/groups` added so the page resolves the user's group(s).
- **Frontend**: `static/group.html` + `group.js` — roster for all; owners get per-member
  two-click Remove and an invite-code panel (seat usage, active/inactive, deactivate,
  create form). All user text via `textContent` (no innerHTML). `/group` linked from the
  profile nav.
- **DoD verified live**: owner views roster with emails, removes a member (who keeps
  their account/profile but loses group access), creates a new code, a member registers
  on it, owner deactivates it and further registration is refused (400); non-owners see
  no emails and 403 on owner endpoints.

---

## Pending / on the horizon

- **Next (Session 3):** metered Claude service (cost controls, `ai_usage` accounting) —
  the prerequisite for the conversational profile-discovery layer.
- Later: recommendation engine, Google Books integration, voting rounds/ballots,
  reading lists, cross-group matching, flags review.
- Not yet: password reset, email verification, Stripe/billing, managed auth (Clerk),
  multi-owner support, group renaming/creation in-app, email notifications.

**Housekeeping noticed (not blocking):**
- `pytest-cov` is used for the coverage report but is not pinned in `requirements.txt`
  (production deps only). Add a `requirements-dev.txt` if/when coverage joins CI.
- `session1_summary.txt` is a stray scratch file in the repo root (untracked). Delete
  when convenient.
- Minor duplication: the random invite-code generator now lives in
  `services/invites.py` (`generate_code`), but `scripts/seed_group.py` still has its own
  copy (left untouched per Session 2 scope). Fold the script onto the shared helper next
  time the seed script is edited.

---

## Blocked / Needs Adam

_None. Add entries here if a dependency won't install, a requirement conflicts with
reality, or a decision is ambiguous — stop and record it rather than guessing._
