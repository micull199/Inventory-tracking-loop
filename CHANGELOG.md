# Changelog

High-level "what shipped" log. One line per slice. Newest at the top.

## Unreleased

- **F2.1** — Admin user-management UI: `/admin/users` is now an HTML page where admins can assign roles and change account status (pending → active, active → disabled). New POST routes `POST /admin/users/{id}/role` and `POST /admin/users/{id}/status` with server-side guards (admin can't demote or disable themselves, can't activate a roleless user). Pending users sort to the top of the list.
- **F2** — Google SSO + users + roles: `users` table (Alembic migration), `Role` and `UserStatus` enums, Authlib OAuth login + callback, signed-cookie sessions, `require_role` dependency, role-gated `/admin/users`, anonymous/pending/welcome index page, dev/test-only login backdoor for Playwright. `SECRET_KEY` and Google credentials are now required when `APP_ENV=prod`.
- **F1** — Project skeleton and verification harness: FastAPI app with `/health`, SQLAlchemy + Alembic wired (SQLite dev), pydantic-settings config, pytest unit + integration + Playwright e2e harness, `make check` green end-to-end.
