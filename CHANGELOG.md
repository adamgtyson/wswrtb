# Changelog

One line per session. Newest first.

- **Session 5** — Real book cards, feedback, and replayable recent searches: the two
  forward-declared tables wired up as declared (two additive indexes, no schema change),
  `PUT`/`DELETE`/`GET /api/feedback` upserting on the existing `(user_id, title)` key with
  a toggle-style delete and a per-user rate limit, stored recent searches whose replay
  spends no AI call and whose write can never fail the request, and a real card UI with
  covers, canonical titles, description toggles and an unmistakable saved-results marker.
  175 tests, 94% coverage.
- **Session 4** — Google Books verification: a single-chokepoint lookup service with
  0.8 word-overlap matching on title AND author, the forward-declared `api_cache` table
  wired up (60-day TTL, no schema change), unverifiable books dropped and replaced
  through Session 3's existing single retry, and a Google Books outage degrading to
  `verified: false` instead of failing the request. 127 tests, 93% coverage.
- **Session 3** — Metered recommendation engine: a single Claude service enforcing a global
  daily cost ceiling plus per-user hourly/daily/free-tier limits derived from `ai_usage`
  (never in-memory), real token/cost logging, and `POST /api/groups/{id}/recommend` with
  server-side member validation, dedup, one bounded retry, and a placeholder ask page.
  92 tests, 92% coverage.
- **Hardening pass (pre-Session 3)** — abuse resistance: SQLite-backed sliding-window
  rate limiting (worker-safe, no in-memory state) on register/login/invite-create, and a
  CORS origin whitelist via `ALLOWED_ORIGINS`; plus seed-script code-generator dedup and
  stray-file cleanup. 41 tests, 89% coverage.
- **Session 2** — Owner/admin group management: `require_owner` authorization, member
  roster (owner-only emails) + removal (owner-protected), invite-code lifecycle
  (list/create/deactivate, multiple active codes), role-aware group route, and a vanilla
  `/group` management page. 32 tests, 89% coverage.
- **Session 1** — Scaffold + code-gated onboarding: FastAPI/SQLite framework, full schema
  (active + forward-declared tables), bcrypt+JWT auth, atomic seat-limited invite-code
  registration, membership authorization dependency, structured preference-profile builder,
  admin seed script, vanilla signup/login/profile pages, and pytest suite (20 tests, 89% coverage).
