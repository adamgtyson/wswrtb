# Changelog

One line per session. Newest first.

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
