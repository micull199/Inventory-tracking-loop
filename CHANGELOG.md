# Changelog

High-level "what shipped" log. One line per slice. Newest at the top.

## Unreleased

- **F2** — Google SSO + users + roles: `users` table (Alembic migration), `Role` and `UserStatus` enums, Authlib OAuth login + callback, signed-cookie sessions, `require_role` dependency, role-gated `/admin/users`, anonymous/pending/welcome index page, dev/test-only login backdoor for Playwright. `SECRET_KEY` and Google credentials are now required when `APP_ENV=prod`.
- **F1** — Project skeleton and verification harness: FastAPI app with `/health`, SQLAlchemy + Alembic wired (SQLite dev), pydantic-settings config, pytest unit + integration + Playwright e2e harness, `make check` green end-to-end.
