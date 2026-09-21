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
    ├── claude_service.py THE only Anthropic caller: cost/rate gates + ai_usage accounting
    └── google_books.py   THE only Google Books caller: verification + api_cache enrichment

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

### Data flow (a recommendation)

1. A member picks who's reading and describes what they want → `POST
   /api/groups/{id}/recommend`.
2. Every selected id is resolved through the group's membership rows (cross-tenant ids
   are a 400 before any spend), and their stored preference columns build the prompt.
3. One metered Claude call via `claude_service` → JSON parsed, schema-validated, deduped,
   and filtered against titles the member named themselves.
4. Each survivor is resolved against Google Books (cache-first, concurrently). No
   confident match → dropped. API unreachable → kept, flagged `verified: false`.
5. Short of five for ANY of those reasons → exactly ONE more Claude call carrying every
   title already tried as an exclusion list. Still short → return the partial set.

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
- **Dev on `tyserver` (the dev server; it appears as "the-rig" in Cowork). Never edit
  on the Droplet.** Production is a
  DigitalOcean Droplet; deploys are pulls, not in-place edits. Keep everything portable
  Ubuntu-standard. Local dev only this session — do not deploy.
- Every function gets a docstring. No magic strings/numbers in logic — use module
  constants.
- The JSON preference columns are the **single source of truth** for a member's
  profile. A future conversational (Claude) discovery layer must augment these SAME
  columns — do not fork the data model.
- **All Anthropic API access goes through `app/services/claude_service.py`.** It is the
  only module that imports the `anthropic` SDK — cost control depends on there being
  exactly one path a request can take.
- **All Google Books access goes through `app/services/google_books.py`.** Same
  single-chokepoint rule, same reason: one place to cache, throttle, or swap the
  provider, and one place to audit.
- **Commit directly to `main`, and PUSH before the session ends.** No branch/PR workflow
  — there's no CI gate or second reviewer to make one useful here. This bullet was
  dropped in the Session 5 doc rewrite and is restored deliberately so it stops being
  re-litigated. The push half is not optional: Session 5 sat committed-but-unpushed for
  two days, so seven commits of finished work lived on exactly one ageing laptop while
  the GitHub-synced project context silently showed Session 4 state. `git push origin
  main` is part of finishing, not part of deploying.

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
`(created_at)` for the per-user count and global daily-sum queries), and `api_cache`
(Session 4 — one row per verified Google Books volume, keyed on a normalized
`google_books:<title>|<author>` string; the `cache_key` primary key is the only index it
needs), and `feedback` + `recent_searches` (Session 5 — ratings keyed on
`(user_id, title)` with no group dimension, and one stored result set per search, indexed
on `user_id` and `(user_id, group_id, created_at)` respectively). `users.plan` is now read
too (free-tier monthly allowance). The rest (`reading_list`, `voting_rounds`, `ballots`,
`flags`) are **forward-declared** — created now, unused. No table or column has ever been
altered, `api_cache`, `feedback` and `recent_searches` included: each fits its feature as
declared in Session 1, and the only additions have been `CREATE INDEX IF NOT EXISTS`.

Invite model: `invite_codes` (seat-limited, multi-redemption) + `invite_redemptions`
(one row per successful redemption, audit trail + UNIQUE guard). This supersedes any
single-use `invites` table. One invite code maps to one group.

---

## Current Build State

_Sessions 1–5 + the pre-Session-3 hardening pass complete. 175 tests passing, 94%
coverage. Live smoke-tested end to end on 2026-09-21 against the real Anthropic and
Google Books APIs._

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
  membership + seat-limited invite code.

**Session 2 — Owner/Admin Member Management (in-app tooling after the first code):**

- **Authorization** (`app/deps.py`): `require_membership(...)` (any member) and
  `require_owner(...)` (role = `'owner'`). Both resolve role server-side from the JWT +
  `memberships` table; owner-only endpoints 403 for members and cross-tenant owners.
- **Member routes** (`app/routes/group_routes.py`): `GET .../members` (roster for all
  members; **emails only for owners**), `DELETE .../members/{user_id}` (owner-only,
  deletes just the membership row, **refuses to remove the owner**, 404 for a non-member).
- **Invite-code routes** (owner-only): `GET` list, `POST` create (optional custom code,
  seats 1–200, 409 on duplicate), `PATCH .../deactivate` (idempotent).
- `GET /api/groups/{id}` includes the caller's `role`; `GET /api/me/groups` added.
- **Frontend**: `static/group.html` + `group.js` — roster for all; owners get two-click
  Remove and an invite-code panel. All user text via `textContent`.

**Pre-Session-3 Hardening Pass — abuse-resistance (not AI cost control):**

- **Rate limiting** (`app/services/rate_limit.py`, `rate_limit_events` table):
  SQLite-backed sliding-window limiter — **deliberately not in-memory**, because
  Gunicorn workers have separate memory (an in-process counter would let each worker
  grant the full quota and reset on restart). `check_and_record(bucket, limit, window)`
  runs count-then-insert inside `BEGIN IMMEDIATE`, evaluates the window in SQL, and
  opportunistically prunes the bucket's expired rows. Wired onto `POST /api/register`
  (by IP, 5/hr), `POST /api/login` (by IP, 10/15min), `POST .../invite-codes` (by owner
  id, 20/hr), and — Session 5 — `PUT`/`DELETE /api/feedback` (by user id, 120/hr).
  Limits are env-configurable; a trip raises `RateLimitError` → global **HTTP 429**.
  `client_ip()` flags that `X-Forwarded-For` handling is deferred until Nginx (Session 7).
- **CORS** (`app/main.py`): `CORSMiddleware` with an explicit `ALLOWED_ORIGINS` whitelist
  from `.env` (no wildcards; `allow_credentials=True`; methods/headers restricted).

**Session 3 — Metered Claude service + recommendation engine:**

- **Metered AI** (`app/services/claude_service.py`): the ONLY module that imports the
  `anthropic` SDK. `complete_text(...)` runs four checks (each its own exception →
  generic **HTTP 429**) before any request: the **global daily cost ceiling**
  (`AI_DAILY_COST_CEILING_USD`, default $5) summed across ALL users for the UTC day — a
  kill switch, not a per-user limit; then the caller's **hourly** (15) and **daily** (50)
  request counts; then, for `plan = 'free'` users, a rolling-30-day count
  (`FREE_TIER_MONTHLY_AI_REQUESTS`, 15 — the binding constraint in practice). Every check
  is a fresh `ai_usage` query — **no in-memory state anywhere**. After a successful call
  it writes one `ai_usage` row with the **real** token counts and `est_cost_usd` from
  named per-MTok pricing constants (`claude-haiku-4-5`: $1 in / $5 out — **must be
  revisited if Anthropic's pricing changes**). A missing `ANTHROPIC_API_KEY` → **503**;
  an upstream/parse failure → **502**. Never degrades silently.
- **Recommendations** (`app/routes/recommend_routes.py`): `POST /api/groups/{id}/recommend`
  behind `require_membership`. Every selected id is resolved through the group's
  membership rows, so a cross-tenant id is a **400** before any spend. The system prompt
  is built from the selected members' stored profile columns (emails never enter a
  prompt) and states genre as a **hard constraint**. Three rules are enforced
  **server-side** rather than trusted to the model: fence-stripping + per-entry Pydantic
  validation (malformed → 502), dedup on normalized `(title, author)`, and a filter
  dropping any title/author named in the raw prompt. Short of five, **exactly one** retry
  carrying the seen titles as exclusions; a failed top-up returns the partial set.

**Session 4 — Google Books verification + metadata enrichment:**

- **Lookup service** (`app/services/google_books.py`): the ONLY module that calls Google
  Books. `lookup(title, author)` has **three** outcomes and callers must handle all
  three: a metadata dict (confident match), `None` (the API answered and nothing matched
  — Claude probably invented the book), and `GoogleBooksUnavailable` (unreachable, which
  is no evidence either way). Conflating the last two is the bug this design prevents.
  **Matching** is word containment over punctuation-stripped, lowercased strings —
  `MATCH_THRESHOLD` 0.8 on **both** title and author, because the difference between
  Claude and Google is almost always extra words (a subtitle, a series tag).
  `GOOGLE_BOOKS_API_KEY` is **optional** in code — the app must never fail to start
  without it — but see the housekeeping note: it is required in practice.
- **`api_cache` wired up**: keyed on `google_books:<normalized title>|<normalized author>`,
  cache-first, upsert, corrupt/expired rows treated as a miss. A cache write that fails is
  logged and ignored. **TTL 60 days** (`API_CACHE_TTL_DAYS`) — book metadata does not
  change, so expiry is insurance against a permanently stale row. **Only confident
  matches are cached.**
- **Wired into the route**: each survivor is verified concurrently (`asyncio.gather`,
  which preserves order). An unverifiable book takes **exactly the same path as a
  duplicate** — its title is already in `seen_titles`, so Session 3's single retry tops
  the set back up. **No second retry loop.** A Google Books outage returns those books
  with `verified: false` rather than 500ing or emptying the page.
- **Response shape** (`app/models.py`): `VerifiedRecommendation` extends `Recommendation`.
  Deliberately a **subclass** rather than optional fields: Claude's raw output is parsed
  as `Recommendation`, which doesn't declare them, so a model that emits its own
  `thumbnail_url` cannot get it into a response.

**Session 5 — Real book cards, feedback, and replayable recent searches:**

- **Feedback** (`app/routes/feedback_routes.py`, `PUT`/`DELETE`/`GET /api/feedback`):
  its own module, not an addition to `recommend_routes.py` — every route there is
  group-scoped, resolves a group id and spends a metered AI call, and feedback is none of
  those. The `feedback` table carries **no `group_id`**, so a rating is **per-user and
  account-global**, authorized by authentication alone; and its uniqueness key is
  **`(user_id, title)`**, which drives the whole design. Writes are upserts on that key
  (re-rating updates in place; the same rating twice is an idempotent no-op), `DELETE` is
  a **toggle** (clearing a rating that was never set is **204, not 404**), and `GET` takes
  the whole card set's titles in one request so five books cost one query. The client must
  send **Claude's** title, never the Google Books canonical one, or it would open a second
  row for the same book. Writes share a per-user limiter bucket
  (`RATE_LIMIT_FEEDBACK`, 120/hr); the read path is not limited because it runs on every
  render. **Store-only: `claude_service.py` and `_build_system_prompt` are untouched** —
  a rating changes what a member sees on a card and nothing else.
- **Recent searches** (`recommend_routes.py` + `db.py`): every successful recommend stores
  one `recent_searches` row — `group_id`, `user_id`, `prompt`, `result_count`, `watching`
  (the selected members' display names; the column name is inherited verbatim from the
  sibling movie app), and `results_json` (the whole result set). **The write can never
  fail the request** — same philosophy as Session 4's `api_cache` write: the member has
  already been charged for the call, so a history failure is logged and swallowed, and a
  test forces it to raise and asserts 200. `GET .../recent-searches` lists the **caller's
  own** rows newest first (capped at `RECENT_SEARCHES_LIMIT = 10`, no payload);
  `GET .../recent-searches/{id}` returns the stored results and **spends no AI call** —
  that is the point of the feature. Both scope by user id AND group id **in the WHERE
  clause**, so another member's search, or the caller's own in a different group, is a
  **404** rather than a 403 and leaks nothing about which ids exist. Stored payloads are
  re-validated through `VerifiedRecommendation` on the way out (a row written by an older
  version of the app is untrusted input like any other); unusable entries are dropped
  rather than 500ing history the member cannot re-run for free. Stored results are
  **deliberately never re-verified** against Google Books — that would spend exactly what
  the replay exists to save.
- **Pruning**: after each insert the helper deletes that member's rows for that group
  beyond `RECENT_SEARCHES_RETAINED` (= limit × 2). Opportunistic and **bucket-local**, the
  same pattern `rate_limit.py` uses — a global sweep would scan the whole table on a
  request path a member is waiting on, to reclaim rows that are bounded anyway. The
  reasoning is in the docstring so a later session doesn't "fix" it into a global sweep.
- **Indexes**: two added, both additive `CREATE INDEX IF NOT EXISTS` — `idx_feedback_user`
  and `idx_recent_searches_user_group_created`. **No table or column has ever been
  altered**, these two tables included: they are used exactly as declared in Session 1.
- **Frontend** (`static/recommend.html`, `js/recommend.js`, `css/style.css`): the Session 3
  placeholder is gone. Real cards — cover or a same-footprint CSS placeholder (so a
  missing image causes no layout shift), **canonical title/author preferred when
  `verified`** with Claude's as the fallback, page count and published date each omitted
  entirely when null, and a collapsed description with a show-more toggle. Thumbs up/down
  per card as an `aria-pressed` pair painted from one batched ratings lookup; updates are
  optimistic and revert on failure. A collapsed **Recent** panel replays a stored set,
  made unmistakable by a dashed accent banner naming the saved date, an accent rule down
  the side of the cards and a changed heading — a member mistaking saved covers for new
  recommendations is the failure mode this feature has. "Ask again" refills the prompt and
  reader checkboxes so spending a fresh call stays a deliberate second action. Every node
  is built with `createElement` and every server-supplied string set via `textContent`;
  all new styles use the existing custom properties, so both themes are covered with no
  new hardcoded colors.
- **Tests**: 48 new (175 total, 94% coverage). The autouse Google Books stub and the
  asserting fake Anthropic client are untouched — no test can reach a real API. Replay
  tests assert on the **fake client's call count**, so a replay that secretly re-asked
  Claude would fail loudly rather than quietly costing money.
- **Live smoke test verified (2026-09-21)**, two days after the code landed. Five real
  cards rendered with covers, canonical titles and page counts; the description toggle,
  dark mode and the saved-results banner all read correctly. The feedback upsert was
  confirmed at the DB level — a thumbs-up then thumbs-down on the same book left
  **exactly one `feedback` row** at `rating = -1`, proving the `(user_id, title)` key is
  being hit and the client sends Claude's title rather than the canonical one. The replay
  spends nothing: `SELECT COUNT(*) FROM ai_usage` was identical either side of clicking a
  Recent entry. Two asks produced exactly two `ai_usage` rows 79 seconds apart (293 input
  tokens each, ~$0.0023 per call) — no top-up retry fired, and the identical prompt size
  across a rate-then-re-ask sequence is incidental confirmation that **feedback is not
  leaking into the prompt**, as specified.

---

## Pending / on the horizon

- **Next (Session 6):** voting rounds and ballots — 5 candidates, matching
  `RECOMMENDATION_COUNT`, so a recommendation set can become a ballot unchanged. The
  `voting_rounds` and `ballots` tables are already forward-declared for it.
- **Deferred, needs its own session:** the conversational (Claude) profile-discovery
  layer. Split out of Session 3 deliberately — it's a registration/profile UX feature
  that depends on the metering built there but does not need to ship with it. It must
  augment the SAME JSON preference columns, never fork the data model.
- Later: reading lists, cross-group matching, flags review, and using stored feedback to
  influence recommendations (the ratings are collected now but deliberately feed nothing;
  the prompt surface stays frozen ahead of the pre-launch security review).
- Not yet: password reset, email verification, Stripe/billing, managed auth (Clerk),
  multi-owner support, group renaming/creation in-app, email notifications.

**Housekeeping / known follow-ups (not blocking):**
- **Google Books can match a real volume that has no cover art.** Seen live on
  2026-09-21: Claude returned "The Expanse: Leviathan Wakes" (it prefixed the series name
  onto a single book), the 0.8 word-containment matcher correctly tolerated the extra
  words — exactly what it was built for — and matched volume `hGAb0gEACAAJ`, a
  metadata-only catalog record with no `imageLinks`. Result: `verified: true`, no cover,
  and the card's placeholder did its job. **Cheap future win:** among confident matches,
  prefer a volume that actually has `imageLinks` before taking the first. No schema change
  and no extra API call — the candidates are already in the response.
- **Claude sometimes decorates a title with its series name** ("The Expanse: Leviathan
  Wakes" rather than "Leviathan Wakes"). Harmless for matching, but it is what steers a
  lookup onto a thin edition record, and it means the stored `feedback.title` carries the
  decorated string. Worth one line in the system prompt asking for a single published
  volume under its own title — but the prompt surface stays **frozen** until the
  pre-launch security review has run.
- **RESOLVED 2026-09-21 — `.env` duplication cleaned up.** `.env` had two entries each
  for `GOOGLE_BOOKS_API_KEY` (first empty, second set) and `API_CACHE_TTL_DAYS`; the app
  worked only because python-dotenv takes the last occurrence. The shadowed earlier lines
  were deleted. The `.env.example` working-tree edit was **discarded**: those two
  variables stay out of the committed template by Adam's explicit decision — do not add
  them back, and do not file their absence as drift. The committed template's only
  Session 5 change is the `RATE_LIMIT_FEEDBACK` pair.
- **An explicit "do not do X" in a session prompt is weak protection.** Session 5's prompt
  said in two places not to add those variables to `.env.example`; the session appended
  them anyway (twice). Enforce this class of constraint with a do-not-touch file list plus
  a clean `git status` in the definition of done, not with prose.
- `pytest-cov` is used for the coverage report but is not pinned in `requirements.txt`
  (production deps only). Add a `requirements-dev.txt` if/when coverage joins CI.
- **At deploy time (Session 7):** implement `X-Forwarded-For` handling in
  `rate_limit.client_ip()` once Nginx fronts the app, and set real production
  `ALLOWED_ORIGINS` — otherwise IP-based limits key off the proxy IP and CORS blocks the
  real domain. Flagged in code.
- `google_books` opens a fresh `httpx.AsyncClient` per lookup (up to five per request)
  rather than reusing a pooled one. Deliberate — a module-level client is shared in-process
  state, which this codebase avoids — and negligible at book-club scale.
- **A `GOOGLE_BOOKS_API_KEY` is effectively REQUIRED in practice.** Verified live at the
  end of Session 4: keyless requests returned `429 Quota exceeded ... 'Queries per day'`
  — the anonymous quota is a shared Google project and was already exhausted. The app
  handles this correctly (a 429 is an outage, so books come back `verified: false` rather
  than being dropped), but that means verification is silently OFF without a key.
- `api_cache` rows are never pruned, only overwritten on re-fetch. One row per distinct
  (title, author) ever recommended — bounded in practice.
- `recent_searches` is pruned per (member, group) on insert; `feedback` is **never**
  pruned — a member's ratings are meant to be permanent, and the row count is bounded by
  how many distinct books they have ever been shown.
- Rate-limit cleanup is per-bucket and opportunistic; a bucket that goes permanently
  silent leaves a few stale rows. Negligible at book-club scale.
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
- **Replayed results go stale by design** — a cover URL can rot and a description can
  change, and nothing re-checks them. That is the cost saving working as intended, and it
  is why the saved-date marker on a replayed set is mandatory rather than decorative.
- Anthropic **structured outputs** (`output_config.format`) are supported on
  `claude-haiku-4-5` and would make JSON parsing near-bulletproof. Not adopted in Session 3
  (defensive fence-stripping + Pydantic was the specified approach); a cheap hardening win.


## Blocked / Needs Adam

_None. Add entries here if a dependency won't install, a requirement conflicts with
reality, or a decision is ambiguous — stop and record it rather than guessing._
