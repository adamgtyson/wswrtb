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
├── main.py            FastAPI entrypoint: routers, CORS, static mount, page routes, health, error handlers
├── db.py              SQLite schema init (CREATE TABLE IF NOT EXISTS) + indexes + async connection helpers
├── auth.py            bcrypt hashing (passlib), JWT issue/verify, get_current_user dependency
├── deps.py            require_membership(...) / require_owner(...) authorization factories
├── models.py          Pydantic request/response schemas + server-side validation
├── routes/
│   ├── auth_routes.py       /api/register (code-gated), /api/login, /api/logout, + page routes
│   ├── profile_routes.py    /api/profile GET/PUT, + /api/groups/{id} (role-aware) demo route
│   ├── group_routes.py      owner tooling: member roster/removal, invite-code lifecycle, /group page
│   └── recommend_routes.py  /api/groups/{id}/recommend + prompt building/parsing, /recommend page
└── services/
    ├── invites.py        Atomic invite-code redemption + account creation + code gen/normalization
    ├── rate_limit.py     SQLite-backed sliding-window limiter (correct across Gunicorn workers)
    └── claude_service.py THE only Anthropic caller: cost/rate gates + ai_usage accounting

static/                signup.html, login.html, profile.html, group.html, recommend.html + css/ + js/
scripts/seed_group.py  CLI to create the first owner, group, and seat-limited invite code
tests/                 pytest suite (onboarding, group management, hardening, AI metering + recs)
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
need no migration. **Actively used:** `users`, `groups`, `memberships`, `invite_codes`,
`invite_redemptions` (onboarding + group management), `rate_limit_events` (hardening
pass — sliding-window limiter, indexed on `(bucket, created_at)`), and `ai_usage`
(Session 3 — one row per successful Claude call; indexed on `(user_id, created_at)` and
`(created_at)` for the per-user count and global daily-sum queries). `users.plan` is now
read too (free-tier monthly allowance). The rest (`feedback`, `reading_list`,
`voting_rounds`, `ballots`, `recent_searches`, `api_cache`, `flags`) are
**forward-declared** — created now, unused. No table or column has ever been altered.

Invite model: `invite_codes` (seat-limited, multi-redemption) + `invite_redemptions`
(one row per successful redemption, audit trail + UNIQUE guard). This supersedes any
single-use `invites` table. One invite code maps to one group.

---

## Current Build State

_Sessions 1–3 + the pre-Session-3 hardening pass complete. 92 tests passing, 92% coverage._

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

**Pre-Session-3 Hardening Pass — abuse-resistance (not AI cost control):**

- **Rate limiting** (`app/services/rate_limit.py`, `rate_limit_events` table):
  SQLite-backed sliding-window limiter — **deliberately not in-memory**, because
  Gunicorn workers have separate memory (an in-process counter would let each worker
  grant the full quota and reset on restart). `check_and_record(bucket, limit, window)`
  runs count-then-insert inside `BEGIN IMMEDIATE` (racing requests on a bucket serialize),
  evaluates the window in SQL, and opportunistically prunes the bucket's expired rows.
  Wired onto `POST /api/register` (by IP, default 5/hr), `POST /api/login` (by IP,
  10/15min), and `POST /api/groups/{id}/invite-codes` (by owner id, 20/hr). Limits are
  env-configurable; a trip raises `RateLimitError` → global **HTTP 429** (generic).
  `client_ip()` flags that `X-Forwarded-For` handling is deferred until the Nginx proxy
  exists (Session 7) — trusting it now would let clients spoof their IP.
- **CORS** (`app/main.py`): `CORSMiddleware` with an explicit `ALLOWED_ORIGINS` whitelist
  from `.env` (no wildcards; `allow_credentials=True` for cookie auth; methods/headers
  restricted to what the app uses). Production must set the real deployed origin(s).
- **DoD verified live**: 6th registration from one IP returns 429 (default limit);
  whitelisted origin gets an echoing `Access-Control-Allow-Origin`, unlisted origin gets
  none; seed script now uses the shared `generate_code`.

**Session 3 — Metered Claude service + recommendation engine:**

- **Metered AI** (`app/services/claude_service.py`): the ONLY module that imports the
  `anthropic` SDK — nothing else may call it directly. `complete_text(...)` runs four
  checks (in order, each its own exception → generic **HTTP 429**) before any request:
  the **global daily cost ceiling** (`AI_DAILY_COST_CEILING_USD`, default $5) summed
  across ALL users for the UTC day — a kill switch, not a per-user limit; then the
  caller's **hourly** (15) and **daily** (50) request counts; then, for `plan = 'free'`
  users, a rolling-30-day count (`FREE_TIER_MONTHLY_AI_REQUESTS`, 15 — the binding
  constraint in practice, since everyone is 'free'). Every check is a fresh `ai_usage`
  query — **no in-memory state anywhere**, same Gunicorn-workers reasoning as
  `rate_limit_events`, but deliberately a **separate table**: that one is the generic
  abuse throttle, this one carries tokens and cost. After a successful call it writes one
  `ai_usage` row with the **real** `input_tokens`/`output_tokens` from the response and
  `est_cost_usd` from named per-MTok pricing constants (`claude-haiku-4-5`: $1 in / $5
  out — **must be revisited if Anthropic's pricing changes**, flagged in code).
  `max_tokens` and the max prompt length are named constants. A missing
  `ANTHROPIC_API_KEY` raises → **503**; an upstream/parse failure → **502**. Never
  degrades silently.
- **Recommendations** (`app/routes/recommend_routes.py`): `POST /api/groups/{id}/recommend`
  behind `require_membership`. Body is `{prompt, member_user_ids}`; every selected id is
  resolved through the group's membership rows, so a cross-tenant id is a **400** before
  any spend. The system prompt is built from the selected members' stored profile columns
  (emails never enter a prompt), states genre as a **hard constraint**, and forbids
  recommending anything the member named themselves. Three rules are then enforced
  **server-side** rather than trusted to the model: markdown fences stripped before
  `json.loads` + per-entry Pydantic validation (malformed → 502, not a crash); dedup on
  normalized `(title, author)`; and a filter dropping any title/author named in the raw
  prompt. Short of five, it makes **exactly one** retry with the seen titles as an
  exclusion list — a failed top-up returns the partial set rather than discarding an
  already-billed call. No `google_books_id` this session (Session 4 adds verification).
- **Frontend**: `static/recommend.html` + `recommend.js` — deliberate placeholder (member
  checkboxes, prompt box, plain-text results), linked from `/group` and `/profile`.
  Session 5 replaces it with real book cards.
- **Tests**: 51 new, **every Anthropic call mocked** — the fake client asserts on any
  unexpected extra call, so a test run can never hit the API or spend from the workspace
  cap. Covers all four limits (incl. one user's spend blocking a different user), usage
  logging with real token counts, cross-tenant rejection, fence-stripping/parsing, and
  dedup + single-retry.

---

## Pending / on the horizon

- **Next (Session 4):** Google Books integration — look up and verify each recommended
  title, add `google_books_id` to the model output, and populate `api_cache`. Until then
  a recommended book is unverified and could be wrong or invented.
- **Deferred, needs its own session:** the conversational (Claude) profile-discovery
  layer. It was split out of Session 3 deliberately — it's a registration/profile UX
  feature that depends on the metering built here but does not need to ship with it. It
  must augment the SAME JSON preference columns, never fork the data model.
- Later: voting rounds/ballots (5 candidates, matching `RECOMMENDATION_COUNT`), reading
  lists, feedback thumbs, cross-group matching, flags review.
- Not yet: password reset, email verification, Stripe/billing, managed auth (Clerk),
  multi-owner support, group renaming/creation in-app, email notifications.

**Housekeeping / known follow-ups (not blocking):**
- `pytest-cov` is used for the coverage report but is not pinned in `requirements.txt`
  (production deps only). Add a `requirements-dev.txt` if/when coverage joins CI.
- **At deploy time (Session 7):** implement `X-Forwarded-For` handling in
  `rate_limit.client_ip()` once Nginx fronts the app, and set real production
  `ALLOWED_ORIGINS` — otherwise IP-based limits key off the proxy IP and CORS blocks the
  real domain. Flagged in code.
- Rate-limit cleanup is per-bucket and opportunistic; a bucket that goes permanently
  silent leaves a few stale rows. Negligible at book-club scale — add a global sweep only
  if the table ever grows unexpectedly.
- **AI pricing constants are hardcoded** in `claude_service.py`. If Anthropic changes
  `claude-haiku-4-5` pricing (or `CLAUDE_MODEL` is pointed at a different model) and the
  constants aren't updated, `est_cost_usd` silently stops reflecting real spend and the
  daily ceiling drifts. The Console workspace hard cap is the backstop.
- `ai_usage` rows are never pruned. That's intentional — it's the cost audit trail — but
  it grows forever; revisit only if the table ever gets large.
- The recommendation prompt interpolates user-supplied display names and preference text,
  an inherent prompt-injection surface. Bounded today by invite-gated membership plus
  schema validation and server-side filtering of the output. Worth revisiting if signup
  ever opens up.
- Anthropic **structured outputs** (`output_config.format`) are supported on
  `claude-haiku-4-5` and would make JSON parsing near-bulletproof. Not adopted this
  session (defensive fence-stripping + Pydantic was the specified approach); a cheap
  hardening win later.

---

## Blocked / Needs Adam

_None. Add entries here if a dependency won't install, a requirement conflicts with
reality, or a decision is ambiguous — stop and record it rather than guessing._
